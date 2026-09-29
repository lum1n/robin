"""Per-account memory facts. Stored encrypted; released through the airlock."""

from __future__ import annotations

from typing import Any

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool
from robin.store import HouseholdStore


class Memory(Capability):
    id = "memory"
    tools = [
        Tool(
            name="memory_remember",
            description="Remember a fact about this person or household for later turns.",
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="memory_recall",
            description="Recall remembered facts matching a query.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="memory_forget",
            description="Forget a remembered fact by id.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("fact", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, *, store: HouseholdStore | None = None, facts: dict[str, list[dict[str, str]]] | None = None) -> None:
        self.store = store
        self._facts = {account: [dict(row) for row in rows] for account, rows in (facts or {}).items()}
        if store is not None and facts is None:
            self._facts = store.load_memory()

    def status(self, account_id: str) -> str:
        return f"memory: {len(self._facts.get(account_id, []))} fact(s)"

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "memory_remember":
            fact = str(arguments.get("fact", "")).strip()
            if not fact or "\n" in fact:
                return "fact must be one line"
            rows = self._facts.setdefault(account_id, [])
            fact_id = str(len(rows) + 1)
            rows.append({"id": fact_id, "fact": fact[:1000]})
            self._save(account_id)
            return f"remembered {fact_id}"
        if tool_name == "memory_recall":
            query = str(arguments.get("query", "")).casefold()
            rows = [
                row
                for row in self._facts.get(account_id, [])
                if not query or query in row.get("fact", "").casefold()
            ]
            if not rows:
                return "No memories matched."
            return Result(text="Memories:", records=rows)
        if tool_name == "memory_forget":
            fact_id = str(arguments.get("id", ""))
            before = self._facts.get(account_id, [])
            kept = [row for row in before if row.get("id") != fact_id]
            self._facts[account_id] = kept
            self._save(account_id)
            return "forgotten" if len(kept) != len(before) else "not found"
        raise NotImplementedError(tool_name)

    def _save(self, account_id: str) -> None:
        if self.store is not None:
            self.store.save_memory(account_id, self._facts.get(account_id, []))
