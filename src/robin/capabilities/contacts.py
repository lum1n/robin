"""CardDAV contacts. Names and emails are tokenized before the model sees them."""

from __future__ import annotations

import json
from base64 import b64encode
from typing import Any, Protocol
from urllib.request import Request, urlopen

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool


class SecretStore(Protocol):
    def reveal(self, account_id: str, name: str) -> str: ...


def contacts_secret(*, url: str, user: str, password: str) -> str:
    return json.dumps({"password": password, "url": url, "user": user}, sort_keys=True)


class CardDAV:
    def __init__(self, secrets: SecretStore, *, fetch: Any = None) -> None:
        self.secrets = secrets
        self._fetch = fetch or urllib_fetch
        self._cache: dict[str, list[dict[str, str]]] = {}

    def connected(self, account_id: str) -> bool:
        return self._credentials(account_id) is not None

    def all(self, account_id: str) -> list[dict[str, str]]:
        creds = self._credentials(account_id)
        if creds is None:
            return []
        if account_id in self._cache:
            return list(self._cache[account_id])
        body = self._fetch(creds["url"], creds["user"], creds["password"])
        rows = _contacts(str(body))
        self._cache[account_id] = rows
        return list(rows)

    def search(self, account_id: str, query: str) -> list[dict[str, str]]:
        needle = query.casefold()
        return [
            row
            for row in self.all(account_id)
            if needle in " ".join(row.values()).casefold()
        ]

    def get(self, account_id: str, contact_id: str) -> dict[str, str] | None:
        for row in self.all(account_id):
            if row.get("id") == contact_id:
                return row
        return None

    def _credentials(self, account_id: str) -> dict[str, str] | None:
        try:
            raw = self.secrets.reveal(account_id, "contacts")
        except KeyError:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        url, user, password = parsed.get("url"), parsed.get("user"), parsed.get("password")
        if not all(isinstance(value, str) and value for value in (url, user, password)):
            return None
        assert isinstance(url, str) and isinstance(user, str) and isinstance(password, str)
        if not url.startswith("https://"):
            return None
        return {"url": url, "user": user, "password": password}


class Contacts(Capability):
    id = "contacts"
    tools = [
        Tool(
            name="contacts_search",
            description="Search this account's contacts by name, email, or phone.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="contacts_get",
            description="Get one contact by id from a prior contacts_search result.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            effect=Effect.READ,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("name", FieldClass.TOKENIZE, label="PERSON"),
        FieldSpec("email", FieldClass.TOKENIZE, label="EMAIL"),
        FieldSpec("phone", FieldClass.TOKENIZE, label="PHONE"),
    ]

    def __init__(self, book: CardDAV) -> None:
        self.book = book

    def status(self, account_id: str) -> str:
        if self.book.connected(account_id):
            return "contacts: connected"
        return "contacts: not connected"

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if not self.book.connected(account_id):
            return "Contacts are not connected."
        if tool_name == "contacts_search":
            rows = self.book.search(account_id, str(arguments.get("query", "")))
            if not rows:
                return "No contacts matched."
            return Result(text="Contacts:", records=rows)
        if tool_name == "contacts_get":
            row = self.book.get(account_id, str(arguments.get("id", "")))
            if row is None:
                return "No contact matched that id."
            return Result(records=[row])
        raise NotImplementedError(tool_name)


def urllib_fetch(url: str, user: str, password: str) -> str:
    request = Request(url, headers={"Authorization": f"Basic {b64encode(f'{user}:{password}'.encode()).decode()}"}, method="GET")
    with urlopen(request, timeout=8) as response:  # noqa: S310
        return response.read().decode()


def _contacts(body: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    index = 0
    for line in body.replace("\r\n", "\n").splitlines():
        if line.upper().startswith("BEGIN:VCARD"):
            index += 1
            current = {"id": str(index), "name": "", "email": "", "phone": ""}
        elif line.upper().startswith("END:VCARD") and current is not None:
            found.append(current)
            current = None
        elif current is not None and line.upper().startswith("FN:"):
            current["name"] = line.split(":", 1)[-1]
        elif current is not None and line.upper().startswith("EMAIL"):
            current["email"] = line.split(":", 1)[-1]
        elif current is not None and line.upper().startswith("TEL"):
            current["phone"] = line.split(":", 1)[-1]
    return found
