"""Push a short notification to the person through the app channel."""

from __future__ import annotations

from typing import Any, Callable

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool
from robin.store import HouseholdStore


class Notify(Capability):
    id = "notify"
    tools = [
        Tool(
            name="notify_person",
            description="Send a short push notification to this person through the Robin app.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [FieldSpec("text", FieldClass.ORDINARY, free_text=True)]

    def __init__(
        self,
        *,
        store: HouseholdStore | None = None,
        deliver: Callable[[str, str], None] | None = None,
    ) -> None:
        self.store = store
        self.deliver = deliver
        self._inbox: dict[str, list[str]] = {}

    def status(self, account_id: str) -> str:
        return "notify: available"

    def pending(self, account_id: str) -> list[str]:
        return list(self._inbox.get(account_id, []))

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name != "notify_person":
            raise NotImplementedError(tool_name)
        text = str(arguments.get("text", "")).strip()
        if not text:
            return "text is required"
        text = text[:500]
        self._inbox.setdefault(account_id, []).append(text)
        if self.store is not None and hasattr(self.store, "save_notification"):
            self.store.save_notification(account_id, text)
        if self.deliver is not None:
            self.deliver(account_id, text)
        return "notified"
