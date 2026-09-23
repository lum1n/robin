"""Calendar connector. CalDAV reads and writes. The secret stays in the broker."""

from __future__ import annotations

import json
from base64 import b64encode
from typing import Any, Protocol
from urllib.request import Request, urlopen

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool

_SECRET = ("url", "user", "password")


class SecretStore(Protocol):
    def reveal(self, account_id: str, name: str) -> str: ...


def calendar_secret(*, url: str, user: str, password: str) -> str:
    return json.dumps({"password": password, "url": url, "user": user}, sort_keys=True)


class CalDAV:
    def __init__(self, secrets: SecretStore, *, fetch: Any = None, put: Any = None) -> None:
        self.secrets = secrets
        self._fetch = fetch or urllib_fetch
        self._put = put or urllib_put

    def events(self, account_id: str) -> list[dict[str, str]]:
        creds = self._credentials(account_id)
        if creds is None:
            return []
        try:
            body = self._fetch(creds["url"], creds["user"], creds["password"])
        except Exception as exc:
            if creds["password"] in str(exc):
                raise RuntimeError("calendar login failed") from None
            raise
        return [_without(row, creds["password"]) for row in _events(str(body))]

    def add(self, account_id: str, title: str, when: str) -> None:
        creds = self._credentials(account_id)
        if creds is None:
            raise RuntimeError("calendar is not connected")
        if "\n" in title or "\r" in title or "\n" in when or "\r" in when:
            raise ValueError("event fields must be one line")
        ics = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            f"SUMMARY:{title}\n"
            f"DTSTART:{when}\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        try:
            self._put(creds["url"], creds["user"], creds["password"], ics)
        except Exception as exc:
            if creds["password"] in str(exc):
                raise RuntimeError("calendar login failed") from None
            raise

    def _credentials(self, account_id: str) -> dict[str, str] | None:
        try:
            raw = self.secrets.reveal(account_id, "calendar")
        except KeyError:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        creds: dict[str, str] = {}
        for key in _SECRET:
            value = parsed.get(key)
            if not isinstance(value, str) or not value:
                return None
            creds[key] = value
        if not creds["url"].startswith("https://"):
            return None
        return creds


class Calendar(Capability):
    id = "agenda"
    tools = [
        Tool(
            name="list_events",
            description="List events for this account.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="add_event",
            description="Add an event on this account's calendar.",
            parameters={
                "type": "object",
                "properties": {"title": {"type": "string"}, "when": {"type": "string"}},
                "required": ["title", "when"],
            },
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [
        FieldSpec("title", FieldClass.ORDINARY, free_text=True),
        FieldSpec("when", FieldClass.ORDINARY),
    ]

    def __init__(self, calendar: CalDAV) -> None:
        self.calendar = calendar

    def records(self, account_id: str) -> list[dict[str, str]]:
        return self.calendar.events(account_id)

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_events":
            return f"{len(self.records(account_id))} events"
        if tool_name == "add_event":
            self.calendar.add(account_id, str(arguments.get("title", "")), str(arguments.get("when", "")))
            return "added"
        raise NotImplementedError(tool_name)


def urllib_fetch(url: str, user: str, password: str) -> str:
    request = Request(url, headers={"Authorization": _basic(user, password)}, method="GET")
    with urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read().decode()


def urllib_put(url: str, user: str, password: str, body: str) -> None:
    request = Request(
        url,
        data=body.encode(),
        headers={"Authorization": _basic(user, password), "Content-Type": "text/calendar"},
        method="PUT",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310
        response.read()


def _basic(user: str, password: str) -> str:
    token = b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def _events(body: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in _unfold(body).splitlines():
        if line == "BEGIN:VEVENT":
            current = {"title": "", "when": ""}
        elif line == "END:VEVENT" and current is not None:
            found.append(current)
            current = None
        elif current is not None and line.startswith("SUMMARY:"):
            current["title"] = line.removeprefix("SUMMARY:")
        elif current is not None and line.startswith("DTSTART"):
            current["when"] = line.split(":", 1)[-1]
    return found


def _unfold(body: str) -> str:
    return body.replace("\r\n ", "").replace("\n ", "").replace("\r\n", "\n")


def _without(row: dict[str, str], password: str) -> dict[str, str]:
    return {key: value.replace(password, "") for key, value in row.items()}
