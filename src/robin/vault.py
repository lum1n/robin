"""Placeholder maps keyed by account and conversation."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken


class VaultAccessError(PermissionError):
    pass


class ReferenceError(ValueError):
    pass


REFERENCE = re.compile(r"\[([A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*)*)_(?:[0-9a-f]{32}_)?[1-9]\d*\]")
REFERENCE_CANDIDATE = re.compile(
    r"\[(?:[A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*)*_|"
    r"(?i:PERSON|EMAIL|PHONE|ADDRESS|ORG|TEXT)(?:[\s:_-]|(?=\]|$)))"
    r"[^\]\r\n]*(?:\]|(?=$|[\r\n]))"
)


@dataclass
class Vault:
    account_id: str
    conversation_id: str
    _values: dict[str, str] = field(default_factory=dict)
    _tokens: dict[tuple[str, str], str] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    _scope: str = field(default_factory=lambda: secrets.token_hex(16))
    _aliases: dict[str, str] = field(default_factory=dict)

    def token(self, label: str, value: str) -> str:
        if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*)*", label) is None:
            raise ValueError("invalid reference type")
        key = (label, value)
        existing = self._tokens.get(key)
        if existing is not None:
            return existing
        self._counts[label] = self._counts.get(label, 0) + 1
        placeholder = f"[{label}_{self._scope}_{self._counts[label]}]"
        self._tokens[key] = placeholder
        self._values[placeholder] = value
        return placeholder

    def canonicalize(self, text: str) -> str:
        """Upgrade references from a locally loaded legacy vault without revealing values."""
        return REFERENCE.sub(lambda match: self._aliases.get(match.group(), match.group()), text)

    def has_reference(self, reference: str) -> bool:
        return reference in self._values or reference in self._aliases

    def restore(self, text: str, *, strict: bool = False) -> str:
        if strict:
            if re.search(r"\[(?:REDACTED|UNRESOLVED)(?:\]|$)", text, re.IGNORECASE):
                raise ReferenceError("Action blocked: withheld data cannot be used as a tool argument.")
            for match in REFERENCE_CANDIDATE.finditer(text):
                if match.group() not in self._values:
                    raise ReferenceError(
                        "Action blocked: unknown, malformed, or foreign reference. "
                        "Use the exact reference from this conversation; do not guess."
                    )

        def replace(match: re.Match[str]) -> str:
            reference = self._aliases.get(match.group(), match.group()) if not strict else match.group()
            return self._values.get(reference, match.group())

        # Substitute only the original input, never references inside a restored value.
        return REFERENCE.sub(replace, text)

    def spans(self, text: str) -> list[tuple[int, int, str]]:
        """Known values must remain protected even when a later detector misses them."""
        spans: list[tuple[int, int, str]] = []
        for reference, value in self._values.items():
            if not value:
                continue
            label = REFERENCE.fullmatch(reference).group(1)
            pattern = re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)")
            spans.extend((match.start(), match.end(), label) for match in pattern.finditer(text))
        return spans

    def contains_value(self, value: str) -> bool:
        return value in self._values.values()

    def dump(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "conversation_id": self.conversation_id,
            "values": dict(self._values),
            "counts": dict(self._counts),
            "scope": self._scope,
            "aliases": dict(self._aliases),
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
        return cls.from_dump(payload)

    @classmethod
    def from_dump(cls, payload: dict) -> Vault:
        vault = cls(str(payload["account_id"]), str(payload["conversation_id"]))
        if "scope" in payload:
            scope = str(payload["scope"])
            if re.fullmatch(r"[0-9a-f]{32}", scope) is None:
                raise VaultAccessError("invalid vault scope")
            vault._scope = scope
        values = {str(key): str(value) for key, value in dict(payload["values"]).items()}
        vault._counts = {str(label): int(count) for label, count in dict(payload["counts"]).items()}
        vault._aliases = {str(key): str(value) for key, value in dict(payload.get("aliases", {})).items()}
        for placeholder, value in values.items():
            match = REFERENCE.fullmatch(placeholder)
            if match is None:
                raise VaultAccessError("invalid vault reference")
            label = match.group(1)
            if re.fullmatch(r"\[" + re.escape(label) + r"_[1-9]\d*\]", placeholder):
                reference = vault.token(label, value)
                vault._aliases[placeholder] = reference
            else:
                if not placeholder.startswith(f"[{label}_{vault._scope}_"):
                    raise VaultAccessError("reference belongs to another vault")
                vault._values[placeholder] = value
                vault._tokens[(label, value)] = placeholder
                vault._counts[label] = max(vault._counts.get(label, 0), int(placeholder.rsplit("_", 1)[1][:-1]))
        if any(
            re.fullmatch(r"\[[A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*)*_[1-9]\d*\]", alias) is None
            or reference not in vault._values
            or REFERENCE.fullmatch(alias).group(1) != REFERENCE.fullmatch(reference).group(1)
            for alias, reference in vault._aliases.items()
        ):
            raise VaultAccessError("invalid legacy vault reference")
        return vault


def seal_export(payload: dict, passphrase: str) -> str:
    if not passphrase:
        raise ValueError("passphrase is required")
    salt = secrets.token_bytes(16)
    token = Fernet(_export_key(passphrase, salt)).encrypt(json.dumps(payload).encode())
    return json.dumps({"body": token.decode(), "salt": salt.hex()})


def open_export(blob: str, passphrase: str) -> dict:
    try:
        wrapper = json.loads(blob)
        payload = json.loads(Fernet(_export_key(passphrase, bytes.fromhex(wrapper["salt"]))).decrypt(wrapper["body"].encode()))
    except (KeyError, ValueError, json.JSONDecodeError, InvalidToken) as exc:
        raise VaultAccessError("export rejected") from exc
    if not isinstance(payload, dict):
        raise VaultAccessError("export rejected")
    return payload


def _export_key(passphrase: str, salt: bytes) -> bytes:
    derived = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return urlsafe_b64encode(derived)


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

    def put(self, vault: Vault) -> None:
        self._vaults[(vault.account_id, vault.conversation_id)] = vault

    def belonging(self, account_id: str) -> list[Vault]:
        return [vault for vault in self._vaults.values() if vault.account_id == account_id]


def new_key() -> bytes:
    return Fernet.generate_key()
