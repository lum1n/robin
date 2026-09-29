"""Calendar connector. CalDAV reads and writes. The secret stays in the broker."""

from __future__ import annotations

import json
from base64 import b64encode
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.request import Request, urlopen

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool

_SECRET = ("url", "user", "password")


class SecretStore(Protocol):
    def reveal(self, account_id: str, name: str) -> str: ...


def calendar_secret(*, url: str, user: str, password: str) -> str:
    return json.dumps({"password": password, "url": url, "user": user}, sort_keys=True)


class CalDAV:
    def __init__(self, secrets: SecretStore, *, fetch: Any = None, put: Any = None, delete: Any = None) -> None:
        self.secrets = secrets
        self._fetch = fetch or urllib_fetch
        self._put = put or urllib_put
        self._delete = delete or urllib_delete

    def connected(self, account_id: str) -> bool:
        return self._credentials(account_id) is not None

    def events(self, account_id: str, *, start: str = "", end: str = "") -> list[dict[str, str]]:
        creds = self._credentials(account_id)
        if creds is None:
            return []
        try:
            body = self._fetch(creds["url"], creds["user"], creds["password"])
        except Exception as exc:
            if creds["password"] in str(exc):
                raise RuntimeError("calendar login failed") from None
            raise
        rows = [_without(row, creds["password"]) for row in _events(str(body))]
        return _filter_range(rows, start=start, end=end)

    def search(self, account_id: str, query: str) -> list[dict[str, str]]:
        needle = query.casefold()
        return [row for row in self.events(account_id) if needle in (row.get("title") or "").casefold()]

    def free_time(self, account_id: str, start: str, end: str, minutes: int) -> list[dict[str, str]]:
        rows = self.events(account_id, start=start, end=end)
        busy = sorted(_parse_when(row.get("when") or "") for row in rows)
        busy = [stamp for stamp in busy if stamp is not None]
        window_start = _parse_when(start) or datetime.now()
        window_end = _parse_when(end) or (window_start + timedelta(days=1))
        slots: list[dict[str, str]] = []
        cursor = window_start
        needed = timedelta(minutes=max(1, minutes))
        for stamp in busy + [window_end]:
            if stamp - cursor >= needed:
                slots.append({"title": "free", "when": cursor.strftime("%Y%m%dT%H%M%S")})
            cursor = max(cursor, stamp + timedelta(minutes=30))
            if cursor >= window_end:
                break
        return slots

    def add(self, account_id: str, title: str, when: str) -> str:
        creds = self._credentials(account_id)
        if creds is None:
            raise RuntimeError("calendar is not connected")
        if "\n" in title or "\r" in title or "\n" in when or "\r" in when:
            raise ValueError("event fields must be one line")
        event_id = f"robin-{abs(hash((title, when))) % 10_000_000}"
        ics = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            f"UID:{event_id}\n"
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
        return event_id

    def update(self, account_id: str, event_id: str, title: str = "", when: str = "") -> None:
        rows = self.events(account_id)
        match = next((row for row in rows if row.get("id") == event_id), None)
        if match is None:
            raise RuntimeError("event not found")
        self.add(account_id, title or match.get("title") or "event", when or match.get("when") or "")

    def delete(self, account_id: str, event_id: str) -> None:
        creds = self._credentials(account_id)
        if creds is None:
            raise RuntimeError("calendar is not connected")
        try:
            self._delete(creds["url"], creds["user"], creds["password"], event_id)
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
            name="calendar_list",
            description="List calendar events. Optional start and end as YYYYMMDD or ISO datetimes.",
            parameters={
                "type": "object",
                "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
            },
            effect=Effect.READ,
        ),
        Tool(
            name="calendar_search",
            description="Search this account's calendar events by title.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="calendar_free_time",
            description="Find free slots of at least minutes between start and end.",
            parameters={
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "minutes": {"type": "integer"},
                },
                "required": ["start", "end", "minutes"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="calendar_add",
            description="Add an event on this account's calendar. Waits for confirmation.",
            parameters={
                "type": "object",
                "properties": {"title": {"type": "string"}, "when": {"type": "string"}},
                "required": ["title", "when"],
            },
            effect=Effect.EXTERNAL,
        ),
        Tool(
            name="calendar_update",
            description="Update an event by id. Waits for confirmation.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "when": {"type": "string"},
                },
                "required": ["id"],
            },
            effect=Effect.EXTERNAL,
        ),
        Tool(
            name="calendar_delete",
            description="Delete an event by id. Waits for confirmation.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("title", FieldClass.ORDINARY, free_text=True),
        FieldSpec("when", FieldClass.ORDINARY),
    ]

    def __init__(self, calendar: CalDAV) -> None:
        self.calendar = calendar

    def status(self, account_id: str) -> str:
        if self.calendar.connected(account_id):
            return "calendar: connected"
        return "calendar: not connected, connect it in the app"

    def records(self, account_id: str) -> list[dict[str, str]]:
        return []

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "calendar_list":
            return self._list(account_id, start=str(arguments.get("start", "")), end=str(arguments.get("end", "")))
        if tool_name == "calendar_search":
            if not self.calendar.connected(account_id):
                return "The calendar is not connected."
            try:
                rows = self.calendar.search(account_id, str(arguments.get("query", "")))
            except Exception as exc:
                return _calendar_failure(exc)
            if not rows:
                return "No events matched."
            return Result(text="Calendar:", records=rows)
        if tool_name == "calendar_free_time":
            if not self.calendar.connected(account_id):
                return "The calendar is not connected."
            try:
                rows = self.calendar.free_time(
                    account_id,
                    str(arguments.get("start", "")),
                    str(arguments.get("end", "")),
                    int(arguments.get("minutes") or 30),
                )
            except Exception as exc:
                return _calendar_failure(exc)
            if not rows:
                return "No free time in that range."
            return Result(text="Free time:", records=rows)
        if tool_name == "calendar_add":
            event_id = self.calendar.add(
                account_id,
                str(arguments.get("title", "")),
                str(arguments.get("when", "")),
            )
            return f"added {event_id}"
        if tool_name == "calendar_update":
            self.calendar.update(
                account_id,
                str(arguments.get("id", "")),
                title=str(arguments.get("title", "")),
                when=str(arguments.get("when", "")),
            )
            return "updated"
        if tool_name == "calendar_delete":
            self.calendar.delete(account_id, str(arguments.get("id", "")))
            return "deleted"
        raise NotImplementedError(tool_name)

    def _list(self, account_id: str, *, start: str = "", end: str = "") -> str | Result:
        if not self.calendar.connected(account_id):
            return "The calendar is not connected."
        try:
            rows = self.calendar.events(account_id, start=start, end=end)
        except Exception as exc:
            return _calendar_failure(exc)
        if not rows:
            return "The calendar has no events."
        return Result(text="Calendar:", records=rows)


def _calendar_failure(exc: Exception) -> str:
    if "login failed" in str(exc).lower():
        return "The calendar did not accept the sign-in."
    return "The calendar did not answer."


def urllib_fetch(url: str, user: str, password: str) -> str:
    request = Request(url, headers={"Authorization": _basic(user, password)}, method="GET")
    with urlopen(request, timeout=8) as response:  # noqa: S310
        return response.read().decode()


def urllib_put(url: str, user: str, password: str, body: str) -> None:
    request = Request(
        url,
        data=body.encode(),
        headers={"Authorization": _basic(user, password), "Content-Type": "text/calendar"},
        method="PUT",
    )
    with urlopen(request, timeout=8) as response:  # noqa: S310
        response.read()


def urllib_delete(url: str, user: str, password: str, event_id: str) -> None:
    target = url.rstrip("/") + f"/{event_id}.ics"
    request = Request(target, headers={"Authorization": _basic(user, password)}, method="DELETE")
    with urlopen(request, timeout=8) as response:  # noqa: S310
        response.read()


def _basic(user: str, password: str) -> str:
    token = b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def _events(body: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    index = 0
    for line in _unfold(body).splitlines():
        if line == "BEGIN:VEVENT":
            index += 1
            current = {"id": str(index), "title": "", "when": ""}
        elif line == "END:VEVENT" and current is not None:
            found.append(current)
            current = None
        elif current is not None and line.startswith("UID:"):
            current["id"] = line.removeprefix("UID:")
        elif current is not None and line.startswith("SUMMARY:"):
            current["title"] = line.removeprefix("SUMMARY:")
        elif current is not None and line.startswith("DTSTART"):
            current["when"] = line.split(":", 1)[-1]
    return found


def _filter_range(rows: list[dict[str, str]], *, start: str, end: str) -> list[dict[str, str]]:
    start_at = _parse_when(start)
    end_at = _parse_when(end)
    if start_at is None and end_at is None:
        return rows
    kept: list[dict[str, str]] = []
    for row in rows:
        when = _parse_when(row.get("when") or "")
        if when is None:
            kept.append(row)
            continue
        if start_at is not None and when < start_at:
            continue
        if end_at is not None and when > end_at:
            continue
        kept.append(row)
    return kept


def _parse_when(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M%SZ", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt.replace("Z", ""))
        except ValueError:
            continue
    return None


def _unfold(body: str) -> str:
    return body.replace("\r\n ", "").replace("\n ", "").replace("\r\n", "\n")


def _without(row: dict[str, str], password: str) -> dict[str, str]:
    return {key: value.replace(password, "") for key, value in row.items()}
