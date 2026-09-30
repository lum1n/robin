"""Push a short notification to the person through the app channel."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Callable

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool
from robin.store import HouseholdStore


class Notify(Capability):
    id = "notify"
    tools = [
        Tool(
            name="notify_person",
            description=(
                "Send a short push notification to this person through the Robin app right now. "
                "Not for later: to remind them at a future time, use jobs_add."
            ),
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("kind", FieldClass.ORDINARY),
        FieldSpec("conversation_id", FieldClass.ORDINARY),
        FieldSpec("text", FieldClass.ORDINARY, free_text=True),
        FieldSpec("created", FieldClass.ORDINARY),
    ]

    def __init__(
        self,
        *,
        store: HouseholdStore | None = None,
        deliver: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.deliver = deliver
        self._inbox: dict[str, list[dict[str, Any]]] = {}
        if store is not None:
            for account_id in store.accounts():
                items = store.list_notifications(account_id)
                if items:
                    self._inbox[account_id] = items

    def status(self, account_id: str) -> str:
        count = len(self._inbox.get(account_id, []))
        if count:
            return f"notify: {count} unread"
        return "notify: available"

    def pending(self, account_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._inbox.get(account_id, [])]

    def enqueue(
        self,
        account_id: str,
        text: str,
        *,
        kind: str = "notify",
        conversation_id: str = "",
    ) -> dict[str, Any]:
        """Record an unread attention item for the app to poll."""
        cleaned = str(text).strip()[:500]
        if not cleaned:
            raise ValueError("text is required")
        if self.store is not None:
            record = self.store.save_notification(
                account_id,
                cleaned,
                kind=kind,
                conversation_id=conversation_id,
            )
        else:
            record = {
                "id": secrets.token_hex(8),
                "kind": kind if kind in {"notify", "confirm", "input"} else "notify",
                "conversation_id": conversation_id,
                "text": cleaned,
                "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }
        self._inbox.setdefault(account_id, []).append(dict(record))
        if self.deliver is not None:
            self.deliver(account_id, dict(record))
        return dict(record)

    def ack(self, account_id: str, ids: list[str]) -> int:
        wanted = {str(item) for item in ids if str(item)}
        if not wanted:
            return 0
        before = self._inbox.get(account_id, [])
        kept = [item for item in before if str(item.get("id") or "") not in wanted]
        removed = len(before) - len(kept)
        if removed:
            self._inbox[account_id] = kept
        if self.store is not None:
            self.store.ack_notifications(account_id, list(wanted))
        return removed

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name != "notify_person":
            raise NotImplementedError(tool_name)
        text = str(arguments.get("text", "")).strip()
        if not text:
            return "text is required"
        self.enqueue(account_id, text, kind="notify")
        return "notified"
