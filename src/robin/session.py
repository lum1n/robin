"""One account's task: views, route, tool calls, and the activity log."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from robin.airlock import UNRESOLVED, VocabularyTerm, redact
from robin.capability import Capability, DueWork, Effect, Registry, SecretAccepted, render_context
from robin.ner import Ner, UnavailableNer
from robin.policy import Decision, Task, decide
from robin.store import HouseholdStore
from robin.vault import Vault, VaultAccessError, VaultStore, open_export, seal_export


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
    store: HouseholdStore | None = None

    def put(self, account_id: str, name: str, value: str) -> None:
        self._secrets[(account_id, name)] = value
        if self.store is not None:
            self.store.save_secret(account_id, name, value)

    def reveal(self, account_id: str, name: str) -> str:
        return self._secrets[(account_id, name)]

    def delete(self, account_id: str, name: str) -> None:
        self._secrets.pop((account_id, name), None)
        if self.store is not None:
            self.store.delete_secret(account_id, name)

    def names(self, account_id: str) -> list[str]:
        return sorted(name for owner, name in self._secrets if owner == account_id)


class Assistant:
    def __init__(self, ner: Ner | None = None, store: HouseholdStore | None = None) -> None:
        self.registry = Registry()
        self.ner = ner or UnavailableNer()
        self.vaults = VaultStore()
        self.activity = ActivityLog()
        self.broker = Broker(store=store)
        self.vocabulary: dict[str, tuple[VocabularyTerm, ...]] = {}
        self.store = store
        self._pending: dict[tuple[str, str], dict] = {}
        self.schedules: dict[str, bool] = {}
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

    def export_account(self, account_id: str, passphrase: str) -> str:
        if self.store is not None:
            vaults = [vault.dump() for vault in self.store.load_vaults() if vault.account_id == account_id]
        else:
            vaults = [vault.dump() for vault in self.vaults.belonging(account_id)]
        terms = self.vocabulary.get(account_id, ())
        payload = {
            "account_id": account_id,
            "vaults": vaults,
            "vocabulary": [{"label": term.label, "text": term.text} for term in terms],
        }
        return seal_export(payload, passphrase)

    def import_account(self, account_id: str, passphrase: str, blob: str) -> int:
        payload = open_export(blob, passphrase)
        if payload.get("account_id") != account_id:
            raise VaultAccessError("export belongs to another account")
        incoming = [Vault.from_dump(item) for item in payload.get("vaults", [])]
        if any(vault.account_id != account_id for vault in incoming):
            raise VaultAccessError("export belongs to another account")
        for vault in incoming:
            self.vaults.put(vault)
            if self.store is not None:
                self.store.save_vault(vault)
        terms = tuple(VocabularyTerm(str(item["text"]), str(item["label"])) for item in payload.get("vocabulary", []))
        self.set_vocabulary(account_id, terms)
        return len(incoming)

    def persist_vault(self, account_id: str, conversation_id: str) -> None:
        if self.store is not None:
            self.store.save_vault(self.vaults.get(account_id, conversation_id))

    def set_pending(
        self,
        account_id: str,
        conversation_id: str,
        tool: str,
        arguments: dict,
        route: str,
        *,
        text: str = "",
        allow_cloud: bool = False,
        free_text: bool = False,
    ) -> None:
        record = {
            "tool": tool,
            "arguments": arguments,
            "route": route,
            "text": text,
            "allow_cloud": allow_cloud,
            "free_text": free_text,
        }
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

    def set_schedule(self, account_id: str, enabled: bool) -> None:
        self.schedules[account_id] = enabled
        if self.store is not None:
            self.store.save_schedule(account_id, enabled)

    def schedule_enabled(self, account_id: str) -> bool:
        return self.schedules.get(account_id, False)

    def scheduled_accounts(self) -> list[str]:
        return sorted(account_id for account_id, enabled in self.schedules.items() if enabled)

    def tools(self, account_id: str, task: str = "") -> list[dict[str, Any]]:
        return self.registry.schemas(account_id, task)

    def prepare(self, account_id: str, task: str) -> str:
        notes = [note for capability in self.registry.for_account(account_id) if (note := capability.prepare(account_id, task))]
        return "\n".join(notes)

    def take_direct(self, account_id: str) -> str:
        notes = [note for capability in self.registry.for_account(account_id) if (note := capability.take_direct(account_id))]
        return "\n".join(notes)

    def accept_secret(self, account_id: str, conversation_id: str, text: str) -> SecretAccepted | None:
        for capability in self.registry.for_account(account_id):
            accepted = capability.accept_secret(account_id, conversation_id, text)
            if accepted is not None:
                return accepted
        return None

    def peel_secret(self, account_id: str, text: str) -> str | None:
        for capability in self.registry.for_account(account_id):
            peeled = capability.peel_secret(account_id, text)
            if peeled is not None:
                return peeled
        return None

    def due(self, now: datetime) -> list[DueWork]:
        found: list[DueWork] = []
        for capability in self.registry._capabilities:
            found.extend(capability.due(now))
        return found

    def decide(self, task: Task, *, record: bool = True) -> Decision:
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
        outgoing = UNRESOLVED if task.free_text and message_report.unresolved else message
        redacted = json.dumps({"message": outgoing, "context": context}, sort_keys=True)
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
        if record and self.store is not None:
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
        logged_input = {key: "" if key in tool.drop_arguments else value for key, value in raw.items()}
        logged, _ = redact(json.dumps(logged_input, sort_keys=True), Vault(account_id, "activity"))
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
        for account_id, name, value in self.store.load_secrets():
            self.broker._secrets[(account_id, name)] = value
        self.schedules.update(self.store.load_schedules())
