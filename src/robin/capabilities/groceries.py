"""Household grocery list. Membership is explicit. A private item stays with its owner."""

from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool
from robin.store import HouseholdStore


class Groceries(Capability):
    id = "pantry"
    tools = [
        Tool(
            name="list_items",
            description="List grocery items this account can see.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="add_private",
            description="Add an item only this account can see.",
            parameters={
                "type": "object",
                "properties": {"item": {"type": "string"}},
                "required": ["item"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="add_shared",
            description="Add an item to the household list.",
            parameters={
                "type": "object",
                "properties": {"item": {"type": "string"}},
                "required": ["item"],
            },
            effect=Effect.EXTERNAL,
        ),
        Tool(
            name="add_member",
            description="Let another account see the household list.",
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

    def visible_to(self, account_id: str) -> bool:
        return True

    def records(self, account_id: str) -> list[dict[str, str]]:
        own = list(self.private.get(account_id, []))
        if account_id in self.members:
            return [*self.shared, *own]
        return own

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_items":
            rows = self.records(account_id)
            if not rows:
                return "The grocery list is empty."
            return "Groceries:\n" + "\n".join(row.get("item", "") for row in rows if row.get("item"))
        if tool_name == "add_private":
            self._add(account_id, str(arguments.get("item", "")), shared=False)
            return "added"
        if tool_name == "add_shared":
            if account_id not in self.members:
                raise PermissionError("not a member")
            self._add(account_id, str(arguments.get("item", "")), shared=True)
            return "added"
        if tool_name == "add_member":
            self._admit(account_id, str(arguments.get("account_id", "")))
            return "added"
        raise NotImplementedError(tool_name)

    def _add(self, account_id: str, item: str, *, shared: bool) -> None:
        _one_line(item)
        row = {"item": item, "loyalty": ""}
        if shared:
            self.shared.append(row)
        else:
            self.private.setdefault(account_id, []).append(row)
        self._save()

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


def _one_line(value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise ValueError("item must be one line")
