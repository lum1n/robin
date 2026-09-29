"""Search this account's conversation history on this machine."""

from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool
from robin.store import HouseholdStore


class History(Capability):
    id = "history"
    tools = [
        Tool(
            name="history_search",
            description="Search this account's past conversation threads for a query.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            effect=Effect.READ,
        ),
    ]
    fields = [
        FieldSpec("thread", FieldClass.ORDINARY),
        FieldSpec("role", FieldClass.ORDINARY),
        FieldSpec("text", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, *, store: HouseholdStore | None = None) -> None:
        self.store = store

    def status(self, account_id: str) -> str:
        return "history: available" if self.store is not None else "history: unavailable"

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name != "history_search":
            raise NotImplementedError(tool_name)
        if self.store is None:
            return "History is unavailable without a household store."
        query = str(arguments.get("query", "")).strip().casefold()
        if not query:
            return "query is required"
        rows: list[dict[str, str]] = []
        for thread in self.store.threads(account_id):
            for turn in self.store.turns(account_id, thread):
                text = turn.get("text") or ""
                if query in text.casefold():
                    rows.append(
                        {
                            "thread": thread,
                            "role": turn.get("role") or "",
                            "text": text[:500],
                        }
                    )
                if len(rows) >= 40:
                    break
            if len(rows) >= 40:
                break
        if not rows:
            return "No history matched."
        return Result(text="History:", records=rows)
