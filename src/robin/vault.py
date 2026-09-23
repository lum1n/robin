"""Placeholder maps keyed by account and conversation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken


class VaultAccessError(PermissionError):
    pass


@dataclass
class Vault:
    account_id: str
    conversation_id: str
    _values: dict[str, str] = field(default_factory=dict)
    _tokens: dict[tuple[str, str], str] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)

    def token(self, label: str, value: str) -> str:
        key = (label, value)
        existing = self._tokens.get(key)
        if existing is not None:
            return existing
        self._counts[label] = self._counts.get(label, 0) + 1
        placeholder = f"[{label}_{self._counts[label]}]"
        self._tokens[key] = placeholder
        self._values[placeholder] = value
        return placeholder

    def restore(self, text: str) -> str:
        restored = text
        for placeholder, value in sorted(self._values.items(), key=lambda item: len(item[0]), reverse=True):
            restored = restored.replace(placeholder, value)
        return restored

    def contains_value(self, value: str) -> bool:
        return value in self._values.values()

    def dump(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "conversation_id": self.conversation_id,
            "values": dict(self._values),
            "counts": dict(self._counts),
        }

    def encrypt(self, key: bytes) -> bytes:
        return Fernet(key).encrypt(json.dumps(self.dump()).encode())

    @classmethod
    def decrypt(cls, blob: bytes, key: bytes, *, account_id: str, conversation_id: str) -> Vault:
        try:
            payload = json.loads(Fernet(key).decrypt(blob))
        except InvalidToken as exc:
            raise VaultAccessError("vault key rejected") from exc
        if payload["account_id"] != account_id or payload["conversation_id"] != conversation_id:
            raise VaultAccessError("vault belongs to another account or conversation")
        vault = cls(account_id, conversation_id)
        values = dict(payload["values"])
        vault._values = values
        vault._counts = {label: int(count) for label, count in payload["counts"].items()}
        for placeholder, value in values.items():
            label = placeholder[1 : placeholder.rfind("_")]
            vault._tokens[(label, value)] = placeholder
        return vault


class VaultStore:
    def __init__(self) -> None:
        self._vaults: dict[tuple[str, str], Vault] = {}

    def get(self, account_id: str, conversation_id: str) -> Vault:
        key = (account_id, conversation_id)
        vault = self._vaults.get(key)
        if vault is None:
            vault = Vault(account_id, conversation_id)
            self._vaults[key] = vault
        return vault


def new_key() -> bytes:
    return Fernet.generate_key()
