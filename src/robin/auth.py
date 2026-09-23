"""Account sessions. The bearer token is shown once and is not stored."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from robin.store import HouseholdStore


class AuthError(Exception):
    pass


class Auth:
    def __init__(self, store: HouseholdStore | None = None, *, token_factory=None) -> None:
        self.store = store
        self._factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._passwords: dict[str, tuple[bytes, bytes]] = {}
        self._sessions: dict[str, str] = {}
        if store is not None:
            self._passwords.update(store.load_passwords())
            self._sessions.update(store.load_sessions())

    def register(self, account_id: str, password: str) -> None:
        if not account_id or not password:
            raise AuthError("account and password are required")
        if account_id in self._passwords:
            raise AuthError("account already has a password")
        salt = secrets.token_bytes(16)
        self._passwords[account_id] = (salt, _hash(password, salt))
        if self.store is not None:
            self.store.save_password(account_id, salt, self._passwords[account_id][1])

    def login(self, account_id: str, password: str) -> str:
        record = self._passwords.get(account_id)
        if record is None or not hmac.compare_digest(_hash(password, record[0]), record[1]):
            raise AuthError("login failed")
        token = str(self._factory())
        digest = _digest(token)
        self._sessions[digest] = account_id
        if self.store is not None:
            self.store.save_session(digest, account_id)
        return token

    def account(self, token: str) -> str | None:
        if not token:
            return None
        return self._sessions.get(_digest(token))


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
