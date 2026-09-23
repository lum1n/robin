from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool


class Groceries(Capability):
    id = "pantry"
    tools = [
        Tool(
            name="list_items",
            description="List grocery items this account can see.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        )
    ]
    fields = [
        FieldSpec("item", FieldClass.ORDINARY),
        FieldSpec("loyalty", FieldClass.DROP),
    ]

    def __init__(self, members: set[str], shared: list[dict[str, str]], private: dict[str, list[dict[str, str]]]) -> None:
        self.members = members
        self.shared = shared
        self.private = private

    def visible_to(self, account_id: str) -> bool:
        return account_id in self.members

    def records(self, account_id: str) -> list[dict[str, str]]:
        return [*self.shared, *self.private.get(account_id, [])]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{len(self.records(account_id))} items"
