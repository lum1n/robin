"""One-time enrollment. The private instance calls home, then the token dies."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from robin.store import HouseholdStore


class EnrollRejected(Exception):
    pass


def account_slug(account_id: str) -> str:
    if not account_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in account_id):
        raise ValueError("account id must be a lowercase slug")
    return account_id


class Enrollment:
    def __init__(self, store: HouseholdStore | None = None, *, token_factory: Any = None) -> None:
        self.store = store
        self._factory = token_factory or (lambda: secrets.token_hex(16))
        self._pending: dict[str, dict] = {}
        self._ready: dict[str, dict] = {}
        if store is not None:
            for record in store.load_instances():
                self._install(record)

    def issue(self, account_id: str) -> str:
        account_id = account_slug(account_id)
        self._drop_pending(account_id)
        token = str(self._factory())
        if not token.isalnum():
            raise ValueError("enroll token must be alphanumeric")
        self._pending[token] = {"account_id": account_id, "token": token, "https_url": None, "ready": False}
        self._persist(account_id)
        return token

    def note_address(self, token: str, https_url: str) -> None:
        pending = self._pending.get(token)
        if pending is None or not isinstance(https_url, str) or not https_url.startswith("https://"):
            raise EnrollRejected("token rejected")
        pending["https_url"] = https_url
        self._persist(pending["account_id"])

    def accept(self, token: str) -> dict:
        pending = self._pending.get(token)
        if pending is None or not pending.get("https_url"):
            raise EnrollRejected("token rejected")
        self._pending.pop(token)
        ready = {"account_id": pending["account_id"], "https_url": pending["https_url"], "ready": True}
        self._ready[pending["account_id"]] = ready
        self._persist(pending["account_id"])
        return dict(ready)

    def discard(self, token: str) -> None:
        pending = self._pending.pop(token, None)
        if pending is None:
            return
        if self.store is not None and pending["account_id"] not in self._ready:
            self.store.delete_instance(pending["account_id"])

    def get(self, account_id: str) -> dict | None:
        ready = self._ready.get(account_id)
        if ready is not None:
            return dict(ready)
        for pending in self._pending.values():
            if pending["account_id"] == account_id and pending.get("https_url"):
                return {"account_id": account_id, "https_url": pending["https_url"], "ready": False}
        return None

    def _drop_pending(self, account_id: str) -> None:
        for token, pending in list(self._pending.items()):
            if pending["account_id"] == account_id:
                self._pending.pop(token)

    def _persist(self, account_id: str) -> None:
        if self.store is None:
            return
        ready = self._ready.get(account_id)
        if ready is not None:
            self.store.save_instance(account_id, ready)
            return
        for pending in self._pending.values():
            if pending["account_id"] == account_id:
                self.store.save_instance(account_id, pending)
                return
        self.store.delete_instance(account_id)

    def _install(self, record: dict) -> None:
        account_id = str(record.get("account_id") or "")
        if record.get("ready") is True:
            self._ready[account_id] = {
                "account_id": account_id,
                "https_url": record.get("https_url"),
                "ready": True,
            }
            return
        token = record.get("token")
        if isinstance(token, str) and token:
            self._pending[token] = {
                "account_id": account_id,
                "token": token,
                "https_url": record.get("https_url"),
                "ready": False,
            }


def enroll_on_boot(token_path: Path | str, joint_path: Path | str, post: Any) -> bool:
    token_file = Path(token_path)
    joint_file = Path(joint_path)
    if not token_file.is_file():
        return False
    token = token_file.read_text().strip()
    if not token.isalnum():
        raise ValueError("enroll token must be alphanumeric")
    if not joint_file.is_file():
        raise RuntimeError("joint url is missing")
    joint = joint_file.read_text().strip()
    post(joint.rstrip("/") + "/v1/enroll", {"token": token})
    token_file.unlink()
    return True


def boot(*, token_path: Path | str, joint_path: Path | str, post: Any, serve: Any) -> None:
    enroll_on_boot(token_path, joint_path, post)
    serve()


def urllib_enroll_post(url: str, body: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode())
