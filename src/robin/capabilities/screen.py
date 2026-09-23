from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool


class Screen(Capability):
    id = "display"
    tools = [
        Tool(
            name="read_screen",
            description="Read the text of this account's screen.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="click",
            description="Click within the task the person just gave.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="submit",
            description="Submit a form or send something from the screen.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [
        FieldSpec("text", FieldClass.ORDINARY, free_text=True),
        FieldSpec("password", FieldClass.DROP),
    ]

    def __init__(self, owner: str, text: str, password: str) -> None:
        self.owner = owner
        self.text = text
        self.password = password
        self.clicked = False
        self.submitted = False

    def visible_to(self, account_id: str) -> bool:
        return account_id == self.owner

    def records(self, account_id: str) -> list[dict[str, str]]:
        if account_id != self.owner:
            return []
        return [{"text": self.text, "password": self.password}]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if account_id != self.owner:
            raise PermissionError(account_id)
        if tool_name == "click":
            self.clicked = True
            return "clicked"
        if tool_name == "submit":
            self.submitted = True
            return "submitted"
        return self.text
