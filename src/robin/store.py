"""Encrypted household state. A restart keeps accounts, threads, and the activity log."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from robin.airlock import VocabularyTerm
from robin.vault import Vault, VaultAccessError


class HouseholdStore:
    def __init__(self, path: Path | str, key: bytes) -> None:
        self.path = Path(path)
        self._key = key
        self._fernet = Fernet(key)
        self._db = sqlite3.connect(self.path)
        self._db.execute("CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY)")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS threads (
                account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                PRIMARY KEY (account_id, conversation_id)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS turns (
                account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                role TEXT NOT NULL,
                body BLOB NOT NULL,
                PRIMARY KEY (account_id, conversation_id, position)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS vaults (
                account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                body BLOB NOT NULL,
                PRIMARY KEY (account_id, conversation_id)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity (
                account_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                body BLOB NOT NULL,
                PRIMARY KEY (account_id, position)
            )
            """
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS vocabulary (account_id TEXT PRIMARY KEY, body BLOB NOT NULL)")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending (
                account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                body BLOB NOT NULL,
                PRIMARY KEY (account_id, conversation_id)
            )
            """
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS instances (account_id TEXT PRIMARY KEY, body BLOB NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS passwords (account_id TEXT PRIMARY KEY, body BLOB NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, body BLOB NOT NULL)")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def ensure_account(self, account_id: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO accounts (id) VALUES (?)", (account_id,))
        self._db.commit()

    def accounts(self) -> list[str]:
        rows = self._db.execute("SELECT id FROM accounts ORDER BY id").fetchall()
        return [row[0] for row in rows]

    def ensure_thread(self, account_id: str, conversation_id: str) -> None:
        self.ensure_account(account_id)
        self._db.execute(
            "INSERT OR IGNORE INTO threads (account_id, conversation_id) VALUES (?, ?)",
            (account_id, conversation_id),
        )
        self._db.commit()

    def threads(self, account_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT conversation_id FROM threads WHERE account_id = ? ORDER BY conversation_id",
            (account_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def append_turn(self, account_id: str, conversation_id: str, role: str, text: str) -> None:
        self.ensure_thread(account_id, conversation_id)
        position = self._db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM turns WHERE account_id = ? AND conversation_id = ?",
            (account_id, conversation_id),
        ).fetchone()[0]
        self._db.execute(
            "INSERT INTO turns (account_id, conversation_id, position, role, body) VALUES (?, ?, ?, ?, ?)",
            (account_id, conversation_id, position, role, self._seal(text)),
        )
        self._db.commit()

    def turns(self, account_id: str, conversation_id: str) -> list[dict[str, str]]:
        rows = self._db.execute(
            """
            SELECT role, body FROM turns
            WHERE account_id = ? AND conversation_id = ?
            ORDER BY position
            """,
            (account_id, conversation_id),
        ).fetchall()
        return [{"role": role, "text": self._open(body)} for role, body in rows]

    def save_vault(self, vault: Vault) -> None:
        self.ensure_thread(vault.account_id, vault.conversation_id)
        blob = vault.encrypt(self._key)
        self._db.execute(
            """
            INSERT INTO vaults (account_id, conversation_id, body) VALUES (?, ?, ?)
            ON CONFLICT (account_id, conversation_id) DO UPDATE SET body = excluded.body
            """,
            (vault.account_id, vault.conversation_id, blob),
        )
        self._db.commit()

    def load_vaults(self) -> list[Vault]:
        rows = self._db.execute("SELECT account_id, conversation_id, body FROM vaults").fetchall()
        return [
            Vault.decrypt(body, self._key, account_id=account_id, conversation_id=conversation_id)
            for account_id, conversation_id, body in rows
        ]

    def append_activity(self, account_id: str, entry: dict[str, str]) -> None:
        self.ensure_account(account_id)
        position = self._db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM activity WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]
        self._db.execute(
            "INSERT INTO activity (account_id, position, body) VALUES (?, ?, ?)",
            (account_id, position, self._seal(json.dumps(entry, sort_keys=True))),
        )
        self._db.commit()

    def load_activity(self, account_id: str) -> list[dict[str, str]]:
        rows = self._db.execute(
            "SELECT body FROM activity WHERE account_id = ? ORDER BY position",
            (account_id,),
        ).fetchall()
        return [json.loads(self._open(row[0])) for row in rows]

    def save_vocabulary(self, account_id: str, terms: tuple[VocabularyTerm, ...]) -> None:
        self.ensure_account(account_id)
        payload = json.dumps([{"text": term.text, "label": term.label} for term in terms])
        self._db.execute(
            """
            INSERT INTO vocabulary (account_id, body) VALUES (?, ?)
            ON CONFLICT (account_id) DO UPDATE SET body = excluded.body
            """,
            (account_id, self._seal(payload)),
        )
        self._db.commit()

    def load_vocabulary(self) -> dict[str, tuple[VocabularyTerm, ...]]:
        rows = self._db.execute("SELECT account_id, body FROM vocabulary").fetchall()
        loaded: dict[str, tuple[VocabularyTerm, ...]] = {}
        for account_id, body in rows:
            terms = tuple(VocabularyTerm(item["text"], item["label"]) for item in json.loads(self._open(body)))
            loaded[account_id] = terms
        return loaded

    def save_pending(self, account_id: str, conversation_id: str, record: dict) -> None:
        self.ensure_thread(account_id, conversation_id)
        self._db.execute(
            """
            INSERT INTO pending (account_id, conversation_id, body) VALUES (?, ?, ?)
            ON CONFLICT (account_id, conversation_id) DO UPDATE SET body = excluded.body
            """,
            (account_id, conversation_id, self._seal(json.dumps(record, sort_keys=True))),
        )
        self._db.commit()

    def take_pending(self, account_id: str, conversation_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT body FROM pending WHERE account_id = ? AND conversation_id = ?",
            (account_id, conversation_id),
        ).fetchone()
        self.clear_pending(account_id, conversation_id)
        if row is None:
            return None
        return json.loads(self._open(row[0]))

    def save_instance(self, account_id: str, record: dict) -> None:
        self.ensure_account(account_id)
        self._db.execute(
            """
            INSERT INTO instances (account_id, body) VALUES (?, ?)
            ON CONFLICT (account_id) DO UPDATE SET body = excluded.body
            """,
            (account_id, self._seal(json.dumps(record, sort_keys=True))),
        )
        self._db.commit()

    def load_instances(self) -> list[dict]:
        rows = self._db.execute("SELECT body FROM instances").fetchall()
        return [json.loads(self._open(row[0])) for row in rows]

    def save_password(self, account_id: str, salt: bytes, hashed: bytes) -> None:
        self.ensure_account(account_id)
        payload = json.dumps({"salt": salt.hex(), "hash": hashed.hex()})
        self._db.execute(
            """
            INSERT INTO passwords (account_id, body) VALUES (?, ?)
            ON CONFLICT (account_id) DO UPDATE SET body = excluded.body
            """,
            (account_id, self._seal(payload)),
        )
        self._db.commit()

    def load_passwords(self) -> dict[str, tuple[bytes, bytes]]:
        rows = self._db.execute("SELECT account_id, body FROM passwords").fetchall()
        loaded: dict[str, tuple[bytes, bytes]] = {}
        for account_id, body in rows:
            record = json.loads(self._open(body))
            loaded[account_id] = (bytes.fromhex(record["salt"]), bytes.fromhex(record["hash"]))
        return loaded

    def save_session(self, token_hash: str, account_id: str) -> None:
        self.ensure_account(account_id)
        self._db.execute(
            """
            INSERT INTO sessions (token_hash, body) VALUES (?, ?)
            ON CONFLICT (token_hash) DO UPDATE SET body = excluded.body
            """,
            (token_hash, self._seal(json.dumps({"account_id": account_id}))),
        )
        self._db.commit()

    def load_sessions(self) -> dict[str, str]:
        rows = self._db.execute("SELECT token_hash, body FROM sessions").fetchall()
        return {token_hash: json.loads(self._open(body))["account_id"] for token_hash, body in rows}

    def delete_instance(self, account_id: str) -> None:
        self._db.execute("DELETE FROM instances WHERE account_id = ?", (account_id,))
        self._db.commit()

    def clear_pending(self, account_id: str, conversation_id: str) -> None:
        self._db.execute(
            "DELETE FROM pending WHERE account_id = ? AND conversation_id = ?",
            (account_id, conversation_id),
        )
        self._db.commit()

    def _seal(self, text: str) -> bytes:
        return self._fernet.encrypt(text.encode())

    def _open(self, blob: bytes) -> str:
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken as exc:
            raise VaultAccessError("store key rejected") from exc
