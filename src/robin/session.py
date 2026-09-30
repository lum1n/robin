"""One account's task: views, route, tool calls, and the activity log."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from robin.airlock import UNRESOLVED, VocabularyTerm, redact
from robin.capability import (
    Capability,
    DueWork,
    Effect,
    Registry,
    Result,
    SecretAccepted,
    current_task,
    render_context,
    render_result,
)
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
        self._turn_lock = threading.Lock()
        self._turn_gen: dict[tuple[str, str], int] = {}
        self._turn_busy: dict[tuple[str, str], threading.Lock] = {}
        if store is not None:
            self._restore_store()

    def begin_turn(self, account_id: str, conversation_id: str) -> int:
        """Bump the conversation generation so an older in-flight turn can stop."""
        key = (account_id, conversation_id)
        with self._turn_lock:
            gen = self._turn_gen.get(key, 0) + 1
            self._turn_gen[key] = gen
            if key not in self._turn_busy:
                self._turn_busy[key] = threading.Lock()
            return gen

    def turn_active(self, account_id: str, conversation_id: str, generation: int) -> bool:
        return self._turn_gen.get((account_id, conversation_id)) == generation

    def conversation_lock(self, account_id: str, conversation_id: str) -> threading.Lock:
        key = (account_id, conversation_id)
        with self._turn_lock:
            if key not in self._turn_busy:
                self._turn_busy[key] = threading.Lock()
            return self._turn_busy[key]

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

    def get_profile(self, account_id: str) -> dict[str, str]:
        from robin.profile import PROFILE_SECRET, empty_profile, parse_profile

        try:
            raw = self.broker.reveal(account_id, PROFILE_SECRET)
        except KeyError:
            return empty_profile()
        return parse_profile(raw)

    def set_profile(self, account_id: str, updates: dict) -> dict[str, str]:
        from robin.profile import PROFILE_SECRET, dump_profile, merge_profile

        merged = merge_profile(self.get_profile(account_id), updates)
        self.broker.put(account_id, PROFILE_SECRET, dump_profile(merged))
        return merged

    def profile_presence(self, account_id: str) -> list[str]:
        from robin.profile import filled_keys

        return filled_keys(self.get_profile(account_id))

    def scheduled_accounts(self) -> list[str]:
        return sorted(account_id for account_id, enabled in self.schedules.items() if enabled)

    def tools(self, account_id: str) -> list[dict[str, Any]]:
        return self.registry.schemas(account_id)

    def statuses(self, account_id: str) -> list[str]:
        return self.registry.statuses(account_id)

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

    def pending_input(self, account_id: str, conversation_id: str):
        return self.registry.pending_input(account_id, conversation_id)

    def accept_input(
        self,
        account_id: str,
        conversation_id: str,
        request_id: str,
        values: dict[str, str],
        *,
        cancel: bool = False,
    ) -> SecretAccepted | None:
        return self.registry.accept_input(
            account_id, conversation_id, request_id, values, cancel=cancel
        )

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
        for_model: bool = False,
    ) -> dict[str, str]:
        capability, tool = self.registry.resolve(account_id, tool_name)
        vault = self.vaults.get(account_id, conversation_id)
        vocabulary = self.vocabulary.get(account_id, ())
        if tool.egress:
            raw = {key: value for key, value in arguments.items()}
            if not confirmed and _egress_needs_restore(raw, vault):
                return {"status": "confirm", "tool": tool_name, "reason": "egress"}
            raw = {key: _restore_arg(value, vault) for key, value in raw.items()}
        else:
            raw = {key: _restore_arg(value, vault) for key, value in arguments.items()}
        logged_input = {
            key: "" if key in tool.drop_arguments else _log_arg(value) for key, value in raw.items()
        }
        logged, _ = redact(json.dumps(logged_input, sort_keys=True, default=str), Vault(account_id, "activity"))
        entry = {"tool": tool_name, "arguments": logged}
        self.activity.append(account_id, entry)
        if self.store is not None:
            self.store.append_activity(account_id, entry)
        if (tool.effect is Effect.EXTERNAL or tool.confirm) and not confirmed:
            return {"status": "confirm", "tool": tool_name}
        if (
            tool_name in {"lesson_save", "lesson_update", "memory_remember"}
            and not confirmed
            and _lesson_needs_confirm()
        ):
            return {"status": "confirm", "tool": tool_name}
        try:
            outcome = capability.invoke(account_id, tool.name, raw)
        except Exception as exc:
            text = str(exc).strip() or "that action failed"
            if self.store is not None:
                self.store.save_vault(vault)
            return {"status": "done", "result": text}
        rendered, _ = render_result(
            outcome,
            capability.fields,
            vault,
            vocabulary=vocabulary,
            ner=self.ner,
            for_cloud=True,
        )
        if isinstance(outcome, Result) and outcome.records is not None:
            text = rendered
        else:
            text = rendered if isinstance(outcome, Result) else str(outcome)
            redacted, _ = redact(text, vault, vocabulary=vocabulary)
            text = redacted
        if self.store is not None:
            self.store.save_vault(vault)
        if for_model or tool.effect is Effect.EXTERNAL:
            return {"status": "done", "result": text}
        return {"status": "done", "result": vault.restore(text)}

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


def _egress_needs_restore(arguments: dict[str, Any], vault: Vault) -> bool:
    """True when an egress argument still holds a placeholder the person must approve."""
    for value in arguments.values():
        if _arg_needs_restore(value, vault):
            return True
    return False


def _arg_needs_restore(value: Any, vault: Vault) -> bool:
    if isinstance(value, str):
        if "[" in value and "]" in value:
            return vault.restore(value) != value
        return False
    if isinstance(value, list):
        return any(_arg_needs_restore(item, vault) for item in value)
    if isinstance(value, dict):
        return any(_arg_needs_restore(item, vault) for item in value.values())
    return False


def _restore_arg(value: Any, vault: Vault) -> Any:
    if isinstance(value, str):
        return vault.restore(value)
    if isinstance(value, list):
        return [_restore_arg(item, vault) for item in value]
    if isinstance(value, dict):
        return {str(key): _restore_arg(item, vault) for key, item in value.items()}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return vault.restore(str(value))


def _log_arg(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_log_arg(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _log_arg(item) for key, item in value.items()}
    return value


def _lesson_needs_confirm() -> bool:
    """Lesson writes from a turn that saw untrusted tool results need a confirm."""
    turn = current_task.get()
    if turn is None:
        return False
    if turn.learning_source != "person":
        return False
    return bool(turn.tainted)
