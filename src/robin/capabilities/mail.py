from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool


class Mail(Capability):
    id = "post"
    tools = [
        Tool(
            name="list_messages",
            description="List messages for this account.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        )
    ]
    fields = [
        FieldSpec("sender", FieldClass.TOKENIZE, label="PERSON"),
        FieldSpec("body", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, boxes: dict[str, list[dict[str, str]]]) -> None:
        self.boxes = boxes

    def records(self, account_id: str) -> list[dict[str, str]]:
        return list(self.boxes.get(account_id, []))

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{len(self.records(account_id))} messages"
