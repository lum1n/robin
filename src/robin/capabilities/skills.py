"""Per-account learned procedures. Drafts wait for confirm before they guide the model."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from robin.airlock import redact
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool, current_task
from robin.store import HouseholdStore
from robin.vault import Vault

_TOOL_NAME = re.compile(r"\b([a-z][a-z0-9_]{2,})\b")
_EXPIRE_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def host_from_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).hostname or ""
    except ValueError:
        return ""
    return host.casefold().removeprefix("www.")


class Skills(Capability):
    id = "skills"
    tools = [
        Tool(
            name="skill_list",
            description="List this account's learned skills (active and proposed drafts).",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            effect=Effect.READ,
        ),
        Tool(
            name="skill_read",
            description="Read the steps of a learned skill by name before following them.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="skill_propose",
            description=(
                "Draft a reusable skill (name, when to use it, markdown steps). "
                "Stores a proposed draft — call skill_activate so the person can confirm it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "steps": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "steps"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="skill_activate",
            description="Activate a proposed skill draft after the person confirms.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            effect=Effect.MUTATE,
            confirm=True,
        ),
        Tool(
            name="skill_retire",
            description="Retire a skill so it is no longer offered.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("name", FieldClass.ORDINARY, free_text=True),
        FieldSpec("description", FieldClass.ORDINARY, free_text=True),
        FieldSpec("steps", FieldClass.ORDINARY, free_text=True),
        FieldSpec("tags", FieldClass.ORDINARY, free_text=True),
        FieldSpec("status", FieldClass.ORDINARY),
        FieldSpec("version", FieldClass.ORDINARY),
    ]

    def __init__(
        self,
        *,
        store: HouseholdStore | None = None,
        skills: dict[str, list[dict[str, Any]]] | None = None,
        known_tools: set[str] | None = None,
    ) -> None:
        self.store = store
        self.known_tools = known_tools or set()
        self._skills: dict[str, list[dict[str, Any]]] = {
            account: [dict(row) for row in rows] for account, rows in (skills or {}).items()
        }
        if store is not None and skills is None:
            self._skills = store.load_skills()

    def status(self, account_id: str) -> str:
        rows = self._skills.get(account_id, [])
        active = sum(1 for row in rows if row.get("status") == "active")
        proposed = sum(1 for row in rows if row.get("status") == "proposed")
        if not active and not proposed:
            return ""
        return f"skills: {active} active, {proposed} proposed"

    def guidance(self, account_id: str, text: str) -> list[str]:
        haystack = (text or "").casefold()
        active = [
            row
            for row in self._skills.get(account_id, [])
            if row.get("status") == "active" and self._matches(row, haystack)
        ]
        if not active:
            return []
        lines = ["Learned skills (skill_read before following; lessons beat skill steps when they conflict):"]
        for row in active[:12]:
            lines.append(f"- {row['name']} — {row.get('description') or ''}")
        return lines

    def for_host(self, account_id: str, host: str, *, status: str = "active") -> list[dict[str, Any]]:
        needle = host.casefold().removeprefix("www.")
        if not needle:
            return []
        return [
            row
            for row in self._skills.get(account_id, [])
            if row.get("status") == status
            and (
                needle in [str(tag).casefold() for tag in row.get("tags") or []]
                or row.get("name", "").casefold().startswith(needle + ":")
            )
        ]

    def propose(
        self,
        account_id: str,
        *,
        name: str,
        description: str,
        steps: str,
        tags: list[str] | None = None,
        source_thread: str = "",
    ) -> str:
        return self._propose(
            account_id,
            {
                "name": name,
                "description": description,
                "steps": steps,
                "tags": tags or [],
                "source_thread": source_thread,
            },
        )

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "skill_list":
            return self._list(account_id, arguments)
        if tool_name == "skill_read":
            return self._read(account_id, arguments)
        if tool_name == "skill_propose":
            return self._propose(account_id, arguments)
        if tool_name == "skill_activate":
            return self._activate(account_id, arguments)
        if tool_name == "skill_retire":
            return self._retire(account_id, arguments)
        raise NotImplementedError(tool_name)

    def expire_old(self, account_id: str, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        kept: list[dict[str, Any]] = []
        dropped = 0
        for row in self._skills.get(account_id, []):
            if row.get("status") != "proposed":
                kept.append(row)
                continue
            created = str(row.get("created") or "")
            try:
                stamp = datetime.fromisoformat(created)
            except ValueError:
                kept.append(row)
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (moment - stamp).days
            if age >= _EXPIRE_DAYS:
                dropped += 1
                continue
            kept.append(row)
        if dropped:
            self._skills[account_id] = kept
            self._save(account_id)
        return dropped

    def _matches(self, row: dict[str, Any], haystack: str) -> bool:
        tags = [str(tag).casefold() for tag in row.get("tags") or []]
        if not tags:
            return True
        name = str(row.get("name") or "").casefold()
        return any(tag in haystack for tag in tags) or any(part and part in haystack for part in name.split(":"))

    def _list(self, account_id: str, arguments: dict[str, Any]) -> str | Result:
        query = str(arguments.get("query", "")).casefold()
        rows = []
        for row in self._skills.get(account_id, []):
            blob = " ".join(
                [
                    str(row.get("name") or ""),
                    str(row.get("description") or ""),
                    " ".join(str(tag) for tag in row.get("tags") or []),
                    str(row.get("status") or ""),
                ]
            ).casefold()
            if query and query not in blob:
                continue
            rows.append(
                {
                    "name": row.get("name") or "",
                    "description": row.get("description") or "",
                    "status": row.get("status") or "",
                    "version": str(row.get("version") or 1),
                    "tags": ", ".join(str(tag) for tag in row.get("tags") or []),
                }
            )
        if not rows:
            return "No skills matched."
        return Result(text="Skills:", records=rows)

    def _read(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name", "")).strip()
        for row in self._skills.get(account_id, []):
            if row.get("name") == name and row.get("status") == "active":
                tags = ", ".join(str(tag) for tag in row.get("tags") or [])
                return (
                    f"Skill: {row['name']}\n"
                    f"When: {row.get('description') or ''}\n"
                    f"Tags: {tags}\n"
                    f"Version: {row.get('version') or 1}\n"
                    f"Steps:\n{row.get('steps') or ''}"
                )
        return "skill not found or not active"

    def _propose(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name", "")).strip()
        description = str(arguments.get("description", "")).strip()
        steps = str(arguments.get("steps", "")).strip()
        if not name or not description or not steps:
            return "name, description, and steps are required"
        if "\n" in name:
            return "name must be one line"
        blocked = self._blocked_text(account_id, f"{description}\n{steps}")
        if blocked:
            return blocked
        unknown = self._unknown_tools(steps)
        if unknown:
            return f"steps name unknown tools: {', '.join(sorted(unknown))}"
        raw_tags = arguments.get("tags") or []
        if isinstance(raw_tags, str):
            tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
        else:
            tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        turn = current_task.get()
        source_thread = str(arguments.get("source_thread") or "")
        if not source_thread and turn is not None:
            source_thread = turn.conversation_id
        rows = self._skills.setdefault(account_id, [])
        version = 1
        for row in rows:
            if row.get("name") == name:
                if row.get("status") == "active" and (row.get("steps") or "") == steps:
                    return f"unchanged active skill {name}"
                version = max(version, int(row.get("version") or 1) + 1)
        rows = [row for row in rows if not (row.get("name") == name and row.get("status") == "proposed")]
        rows.append(
            {
                "name": name[:200],
                "description": description[:500],
                "steps": steps[:8000],
                "tags": tags,
                "version": version,
                "status": "proposed",
                "source_thread": source_thread,
                "created": _now(),
            }
        )
        self._skills[account_id] = rows
        self._save(account_id)
        return f"proposed {name} v{version} — ask the person to confirm with skill_activate"

    def _activate(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name", "")).strip()
        rows = self._skills.get(account_id, [])
        draft = next((row for row in rows if row.get("name") == name and row.get("status") == "proposed"), None)
        if draft is None:
            return "proposed skill not found"
        kept = [
            row
            for row in rows
            if not (row.get("name") == name and row.get("status") in {"active", "proposed"})
        ]
        draft = dict(draft)
        draft["status"] = "active"
        kept.append(draft)
        self._skills[account_id] = kept
        self._save(account_id)
        return f"activated {name} v{draft.get('version') or 1}"

    def _retire(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name", "")).strip()
        before = self._skills.get(account_id, [])
        kept = [row for row in before if row.get("name") != name]
        self._skills[account_id] = kept
        self._save(account_id)
        return "retired" if len(kept) != len(before) else "not found"

    def _unknown_tools(self, steps: str) -> set[str]:
        if not self.known_tools:
            return set()
        found = set(_TOOL_NAME.findall(steps))
        # Only flag tokens that look like robin tool names (prefix_snake).
        candidates = {name for name in found if "_" in name}
        return candidates - self.known_tools

    def _blocked_text(self, account_id: str, text: str) -> str:
        _, report = redact(text, Vault(account_id, "skills"))
        if report.has_critical:
            return "refused: skill must not contain secrets or national IDs"
        return ""

    def _save(self, account_id: str) -> None:
        if self.store is not None:
            self.store.save_skills(account_id, self._skills.get(account_id, []))
