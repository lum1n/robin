from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool


class Calendar(Capability):
    id = "agenda"
    tools = [
        Tool(
            name="list_events",
            description="List events for this account.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        )
    ]
    fields = [
        FieldSpec("title", FieldClass.ORDINARY, free_text=True),
        FieldSpec("when", FieldClass.ORDINARY),
    ]

    def __init__(self, events: dict[str, list[dict[str, str]]]) -> None:
        self.events = events

    def records(self, account_id: str) -> list[dict[str, str]]:
        return list(self.events.get(account_id, []))

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{len(self.records(account_id))} events"
