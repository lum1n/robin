"""One account's task: views, route, tool calls, and the activity log."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from robin.airlock import VocabularyTerm, redact
from robin.capability import Capability, Effect, Registry, render_context
from robin.ner import Ner, UnavailableNer
from robin.policy import Decision, Task, decide
from robin.store import HouseholdStore
from robin.vault import Vault, VaultStore


@dataclass
class ActivityLog:
    _entries: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def append(self, account_id: str, entry: dict[str, str]) -> None:
        self._entries.setdefault(account_id, []).append(entry)

    def read(self, account_id: str) -> list[dict[str, str]]:
        return list(self._entries.get(account_id, []))


@dataclass
class Broker:
    """Credentials the model is never given. Execution code is the only reader."""

    _secrets: dict[tuple[str, str], str] = field(default_factory=dict)

    def put(self, account_id: str, name: str, value: str) -> None:
        self._secrets[(account_id, name)] = value

    def reveal(self, account_id: str, name: str) -> str:
        return self._secrets[(account_id, name)]


class Assistant:
    def __init__(self, ner: Ner | None = None, store: HouseholdStore | None = None) -> None:
        self.registry = Registry()
        self.ner = ner or UnavailableNer()
        self.vaults = VaultStore()
        self.activity = ActivityLog()
        self.broker = Broker()
        self.vocabulary: dict[str, tuple[VocabularyTerm, ...]] = {}
        self.store = store
        self._pending: dict[tuple[str, str], dict] = {}
        if store is not None:
            self._restore_store()

    def add(self, capability: Capability) -> None:
        self.registry.add(capability)

    def set_vocabulary(self, account_id: str, terms: tuple[VocabularyTerm, ...]) -> None:
        self.vocabulary[account_id] = terms
        if self.store is not None:
            self.store.save_vocabulary(account_id, terms)

    def threads(self, account_id: str) -> list[str]:
        if self.store is None:
            return []
        return self.store.threads(account_id)

    def turns(self, account_id: str, conversation_id: str) -> list[dict[str, str]]:
        if self.store is None:
            return []
        return self.store.turns(account_id, conversation_id)

    def remember(self, account_id: str, conversation_id: str, role: str, text: str) -> None:
        if self.store is not None:
            self.store.append_turn(account_id, conversation_id, role, text)

    def persist_vault(self, account_id: str, conversation_id: str) -> None:
        if self.store is not None:
            self.store.save_vault(self.vaults.get(account_id, conversation_id))

    def set_pending(self, account_id: str, conversation_id: str, tool: str, arguments: dict, route: str) -> None:
        record = {"tool": tool, "arguments": arguments, "route": route}
        self._pending[(account_id, conversation_id)] = record
        if self.store is not None:
            self.store.save_pending(account_id, conversation_id, record)

    def take_pending(self, account_id: str, conversation_id: str) -> dict | None:
        record = self._pending.pop((account_id, conversation_id), None)
        if self.store is None:
            return record
        stored = self.store.take_pending(account_id, conversation_id)
        return record or stored

    def clear_pending(self, account_id: str, conversation_id: str) -> None:
        self._pending.pop((account_id, conversation_id), None)
        if self.store is not None:
            self.store.clear_pending(account_id, conversation_id)

    def tools(self, account_id: str) -> list[dict[str, Any]]:
        return self.registry.schemas(account_id)

    def decide(self, task: Task) -> Decision:
        vault = self.vaults.get(task.account_id, task.conversation_id)
        vocabulary = self.vocabulary.get(task.account_id, ())
        context, context_report = render_context(
            self.registry.for_account(task.account_id),
            task.account_id,
            vault,
            vocabulary=vocabulary,
            ner=self.ner,
            for_cloud=True,
        )
        message, message_report = redact(
            task.text,
            vault,
            vocabulary=vocabulary,
            free_text=task.free_text,
            ner_available=self.ner.available(),
            extra=self.ner.detect(task.text) if self.ner.available() else (),
        )
        redacted = json.dumps({"message": message, "context": context}, sort_keys=True)
        local_context, _ = render_context(
            self.registry.for_account(task.account_id),
            task.account_id,
            vault,
            vocabulary=vocabulary,
            ner=self.ner,
            for_cloud=False,
        )
        local_message = vault.restore(message)
        local_text = json.dumps({"message": local_message, "context": local_context}, sort_keys=True)
        if self.store is not None:
            self.store.append_turn(task.account_id, task.conversation_id, "user", task.text)
            self.store.save_vault(vault)
        return decide(task, context_report.merge(message_report), redacted=redacted, local_text=local_text)

    def invoke(
        self,
        account_id: str,
        conversation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool = False,
    ) -> dict[str, str]:
        capability, tool = self.registry.resolve(account_id, tool_name)
        vault = self.vaults.get(account_id, conversation_id)
        raw = {key: vault.restore(str(value)) for key, value in arguments.items()}
        logged, _ = redact(json.dumps(raw, sort_keys=True), Vault(account_id, "activity"))
        entry = {"tool": tool_name, "arguments": logged}
        self.activity.append(account_id, entry)
        if self.store is not None:
            self.store.append_activity(account_id, entry)
        if tool.effect is Effect.EXTERNAL and not confirmed:
            return {"status": "confirm", "tool": tool_name}
        result = capability.invoke(account_id, tool.name, raw)
        redacted, _ = redact(result, vault, vocabulary=self.vocabulary.get(account_id, ()))
        if self.store is not None:
            self.store.save_vault(vault)
        if tool.effect is Effect.EXTERNAL:
            return {"status": "done", "result": redacted}
        return {"status": "done", "result": vault.restore(redacted)}

    def _restore_store(self) -> None:
        assert self.store is not None
        self.vocabulary.update(self.store.load_vocabulary())
        for account_id in self.store.accounts():
            for entry in self.store.load_activity(account_id):
                self.activity.append(account_id, entry)
        for vault in self.store.load_vaults():
            self.vaults.put(vault)
