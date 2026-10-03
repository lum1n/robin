"""Household lists (groceries, todo). Membership is explicit. A private item stays with its owner."""

from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool
from robin.store import HouseholdStore

_LISTS = ("groceries", "todo")


class Lists(Capability):
    id = "lists"
    tools = [
        Tool(
            name="lists_show",
            description="Show items on a list this account can see. list is groceries or todo.",
            parameters={
                "type": "object",
                "properties": {"list": {"type": "string", "enum": list(_LISTS)}},
                "required": ["list"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="lists_add",
            description="Add an item to a private list (only this account sees it).",
            parameters={
                "type": "object",
                "properties": {
                    "list": {"type": "string", "enum": list(_LISTS)},
                    "item": {"type": "string"},
                },
                "required": ["list", "item"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="lists_add_shared",
            description="Add an item to a shared household list.",
            parameters={
                "type": "object",
                "properties": {
                    "list": {"type": "string", "enum": list(_LISTS)},
                    "item": {"type": "string"},
                },
                "required": ["list", "item"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="lists_check",
            description="Check off or remove an item from a private list.",
            parameters={
                "type": "object",
                "properties": {
                    "list": {"type": "string", "enum": list(_LISTS)},
                    "item": {"type": "string"},
                },
                "required": ["list", "item"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="lists_check_shared",
            description="Check off or remove an item from a shared list.",
            parameters={
                "type": "object",
                "properties": {
                    "list": {"type": "string", "enum": list(_LISTS)},
                    "item": {"type": "string"},
                },
                "required": ["list", "item"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="lists_add_member",
            description="Let another account see the household shared lists. Waits for confirmation.",
            parameters={
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"],
            },
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [
        FieldSpec("item", FieldClass.ORDINARY),
        FieldSpec("list", FieldClass.ORDINARY),
        FieldSpec("loyalty", FieldClass.DROP),
    ]

    def __init__(
        self,
        members: set[str] | None = None,
        shared: list[dict[str, str]] | None = None,
        private: dict[str, list[dict[str, str]]] | None = None,
        *,
        store: HouseholdStore | None = None,
    ) -> None:
        self.store = store
        self.members = set(members or [])
        self.shared = [dict(row) for row in (shared or [])]
        self.private = {account_id: [dict(row) for row in rows] for account_id, rows in (private or {}).items()}
        if store is None:
            return
        if members is None and shared is None and private is None:
            loaded = store.load_pantry()
            if loaded is not None:
                self.members = set(loaded["members"])
                self.shared = loaded["shared"]
                self.private = loaded["private"]
            return
        self._save()

    def status(self, account_id: str) -> str:
        if account_id in self.members:
            return "lists: household member"
        return "lists: private only"

    def brief(self, account_id: str, text: str = "") -> list[str]:
        rows = self.records(account_id)
        if not rows:
            return []
        grouped: dict[str, list[str]] = {}
        for row in rows:
            name = row.get("list") or "groceries"
            item = (row.get("item") or "").strip()
            if item:
                grouped.setdefault(name, []).append(item)
        lines = ["Lists:"]
        for name, items in grouped.items():
            shown = items[:20]
            extra = f" (+{len(items) - 20} more)" if len(items) > 20 else ""
            lines.append(f"- {name}: {', '.join(shown)}{extra}")
        return lines

    def visible_to(self, account_id: str) -> bool:
        return True

    def records(self, account_id: str) -> list[dict[str, str]]:
        own = list(self.private.get(account_id, []))
        if account_id in self.members:
            return [*self.shared, *own]
        return own

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        name = _list_name(str(arguments.get("list", "groceries")))
        if tool_name == "lists_show":
            rows = [row for row in self.records(account_id) if (row.get("list") or "groceries") == name]
            if not rows:
                return f"The {name} list is empty."
            return f"{name.title()}:\n" + "\n".join(row.get("item", "") for row in rows if row.get("item"))
        if tool_name == "lists_add":
            self._add(account_id, name, str(arguments.get("item", "")), shared=False)
            return "added"
        if tool_name == "lists_add_shared":
            if account_id not in self.members:
                raise PermissionError("not a member")
            self._add(account_id, name, str(arguments.get("item", "")), shared=True)
            return "added"
        if tool_name == "lists_check":
            return self._check(account_id, name, str(arguments.get("item", "")), shared=False)
        if tool_name == "lists_check_shared":
            if account_id not in self.members:
                raise PermissionError("not a member")
            return self._check(account_id, name, str(arguments.get("item", "")), shared=True)
        if tool_name == "lists_add_member":
            self._admit(account_id, str(arguments.get("account_id", "")))
            return "added"
        raise NotImplementedError(tool_name)

    def _add(self, account_id: str, name: str, item: str, *, shared: bool) -> None:
        _one_line(item)
        row = {"item": item, "list": name, "loyalty": ""}
        if shared:
            self.shared.append(row)
        else:
            self.private.setdefault(account_id, []).append(row)
        self._save()

    def _check(self, account_id: str, name: str, item: str, *, shared: bool) -> str:
        needle = item.casefold()
        if shared:
            before = len(self.shared)
            self.shared = [
                row
                for row in self.shared
                if not ((row.get("list") or "groceries") == name and (row.get("item") or "").casefold() == needle)
            ]
            changed = before != len(self.shared)
        else:
            rows = self.private.get(account_id, [])
            before = len(rows)
            kept = [
                row
                for row in rows
                if not ((row.get("list") or "groceries") == name and (row.get("item") or "").casefold() == needle)
            ]
            self.private[account_id] = kept
            changed = before != len(kept)
        self._save()
        return "checked" if changed else "not found"

    def _admit(self, account_id: str, other: str) -> None:
        _one_line(other)
        if self.members and account_id not in self.members:
            raise PermissionError("not a member")
        if not self.members and other != account_id:
            raise PermissionError("not a member")
        self.members.add(other)
        self._save()

    def _save(self) -> None:
        if self.store is None:
            return
        self.store.save_pantry(
            {
                "members": sorted(self.members),
                "private": self.private,
                "shared": self.shared,
            }
        )


# Backwards-compatible alias used by older tests and docs.
Groceries = Lists


def _list_name(value: str) -> str:
    name = (value or "groceries").strip().casefold()
    if name not in _LISTS:
        return "groceries"
    return name


def _one_line(value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise ValueError("item must be one line")
