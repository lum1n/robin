"""One account's task: views, route, tool calls, and the activity log."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from robin.airlock import VocabularyTerm, redact
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
from robin.context import ClientContext, merge_context
from robin.ner import Ner, PublicTerms, UnavailableNer
from robin.policy import Decision, Task, decide
from robin.store import HouseholdStore
from robin.vault import (
    REFERENCE,
    ReferenceError,
    Vault,
    VaultAccessError,
    VaultStore,
    open_export,
    seal_export,
)


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
        self._traces: dict[tuple[str, str], dict] = {}
        self.schedules: dict[str, bool] = {}
        self._turn_lock = threading.Lock()
        self._turn_gen: dict[tuple[str, str], int] = {}
        self._turn_busy: dict[tuple[str, str], threading.Lock] = {}
        # Last device context per account, in memory only (location is never written to the store).
        self._client_context: dict[str, ClientContext] = {}
        # Decrypted household names, cached so each redaction does not hit the broker.
        self._household_terms: dict[str, tuple[VocabularyTerm, ...]] = {}
        if store is not None:
            self._restore_store()

    def set_client_context(self, account_id: str, context: ClientContext) -> None:
        with self._turn_lock:
            self._client_context[account_id] = merge_context(self._client_context.get(account_id), context)

    def _egress_approved(self, account_id: str) -> frozenset[str]:
        """Coarse device place names may reach search providers; precise coordinates still need approval."""
        context = self.client_context(account_id)
        if context is None or context.location is None:
            return frozenset()
        place = context.location
        return frozenset(value for value in (place.locality, place.region, place.country) if value)

    def client_context(self, account_id: str) -> ClientContext | None:
        return self._client_context.get(account_id)

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

    def vocabulary_for(self, account_id: str) -> tuple[VocabularyTerm, ...]:
        """Stored terms plus household names, so every mention maps to the same reference."""
        terms = self._household_terms.get(account_id)
        if terms is None:
            from robin.household import member_terms

            terms = member_terms(self.household(account_id))
            self._household_terms[account_id] = terms
        return (*terms, *self.vocabulary.get(account_id, ()))

    def household(self, account_id: str) -> tuple:
        from robin.household import HOUSEHOLD_SECRET, load_members

        try:
            return load_members(self.broker.reveal(account_id, HOUSEHOLD_SECRET))
        except KeyError:
            return ()

    def set_household(self, account_id: str, members: tuple) -> None:
        from robin.household import HOUSEHOLD_SECRET, dump_members, member_terms

        self.broker.put(account_id, HOUSEHOLD_SECRET, dump_members(members))
        self._household_terms[account_id] = member_terms(members)

    def preferences(self, account_id: str):
        from robin.household import PREFERENCES_SECRET, Preferences, load_preferences

        try:
            return load_preferences(self.broker.reveal(account_id, PREFERENCES_SECRET))
        except KeyError:
            return Preferences()

    def set_preferences(self, account_id: str, preferences) -> None:
        from robin.household import PREFERENCES_SECRET, dump_preferences

        self.broker.put(account_id, PREFERENCES_SECRET, dump_preferences(preferences))

    def threads(self, account_id: str) -> list[str]:
        if self.store is None:
            return []
        return self.store.threads(account_id)

    def delete_thread(self, account_id: str, conversation_id: str) -> bool:
        self._traces.pop((account_id, conversation_id), None)
        if self.store is None:
            return False
        return self.store.delete_thread(account_id, conversation_id)

    def thread_trace(self, account_id: str, conversation_id: str) -> dict:
        record = self._traces.get((account_id, conversation_id))
        if record is None and self.store is not None:
            record = self.store.load_trace(account_id, conversation_id)
            if record is not None:
                self._traces[(account_id, conversation_id)] = record
        return dict(record) if record else {}

    def remember_step(
        self,
        account_id: str,
        conversation_id: str,
        *,
        tool: str,
        arguments: str,
        result: str,
        url: str = "",
    ) -> None:
        """Append one compact, already-redacted tool step to this thread's trail."""
        key = (account_id, conversation_id)
        record = dict(self.thread_trace(account_id, conversation_id))
        steps = [dict(item) for item in record.get("steps") or [] if isinstance(item, dict)]
        step = {
            "tool": tool,
            "arguments": arguments,
            "result": result,
            "url": url,
        }
        steps.append(step)
        record["steps"] = steps[-24:]
        if url:
            record["url"] = url
        elif "url" not in record:
            record["url"] = ""
        self._traces[key] = record
        if self.store is not None:
            self.store.save_trace(account_id, conversation_id, record)

    def set_thread_task(
        self,
        account_id: str,
        conversation_id: str,
        *,
        task: str,
        open_task: bool,
        remaining: list[str] | None = None,
    ) -> None:
        key = (account_id, conversation_id)
        record = dict(self.thread_trace(account_id, conversation_id))
        record["task"] = task
        record["open"] = open_task
        record["remaining"] = list(remaining or [])
        if "steps" not in record:
            record["steps"] = []
        if "url" not in record:
            record["url"] = ""
        self._traces[key] = record
        if self.store is not None:
            self.store.save_trace(account_id, conversation_id, record)

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

    def ner_for(self, account_id: str) -> Ner:
        """NER that leaves this account's own setup names, such as MCP servers, unmasked."""
        terms = self.registry.public_terms(account_id)
        return PublicTerms(self.ner, terms) if terms else self.ner

    def statuses(self, account_id: str) -> list[str]:
        return self.registry.statuses(account_id)

    def overview(self, account_id: str) -> dict[str, Any]:
        """Read-only assistant snapshot for apps — no secrets or profile values."""
        capabilities: list[dict[str, Any]] = []
        mcp: list[dict[str, Any]] = []
        for capability in self.registry.for_account(account_id):
            tools = [tool.name for tool in capability.available_tools(account_id)]
            capabilities.append(
                {
                    "id": capability.id,
                    "status": capability.status(account_id).strip(),
                    "tools": tools,
                }
            )
            summary = getattr(capability, "summary", None)
            if capability.id == "mcp" and callable(summary):
                mcp = summary(account_id)
        connectable = ("calendar", "mailbox")
        connected = [name for name in self.broker.names(account_id) if name in connectable]
        return {
            "account_id": account_id,
            "schedule_enabled": self.schedule_enabled(account_id),
            "connected": connected,
            "profile_present": self.profile_presence(account_id),
            "mcp": mcp,
            "capabilities": capabilities,
            "connectors": self.statuses(account_id),
            "threads": self.threads(account_id),
        }

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
        ner = self.ner_for(task.account_id)
        vocabulary = self.vocabulary_for(task.account_id)
        context, context_report = render_context(
            self.registry.for_account(task.account_id),
            task.account_id,
            vault,
            vocabulary=vocabulary,
            ner=ner,
            for_cloud=True,
        )
        message, message_report = redact(
            task.text,
            vault,
            vocabulary=vocabulary,
            free_text=task.free_text,
            ner_available=ner.available(),
            extra=ner.detect(task.text) if ner.available() else (),
        )
        outgoing = message
        redacted = json.dumps({"message": outgoing, "context": context}, sort_keys=True)
        local_context, _ = render_context(
            self.registry.for_account(task.account_id),
            task.account_id,
            vault,
            vocabulary=vocabulary,
            ner=ner,
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
        vocabulary = self.vocabulary_for(account_id)
        try:
            raw = _restore_arg(arguments, vault)
        except ReferenceError as exc:
            entry = {"tool": tool_name, "arguments": "blocked: invalid reference"}
            self.activity.append(account_id, entry)
            if self.store is not None:
                self.store.append_activity(account_id, entry)
            return {"status": "error", "result": str(exc)}
        if not confirmed and self.confirm_reason(account_id, conversation_id, tool_name, arguments) == "egress":
            return {"status": "confirm", "tool": tool_name, "reason": "egress"}
        logged_input = {
            redact(key, vault, vocabulary=vocabulary)[0]: (
                "" if key in tool.drop_arguments else _redact_value(value, vault, vocabulary)
            )
            for key, value in raw.items()
        }
        logged, _ = redact(
            json.dumps(logged_input, sort_keys=True, default=str), vault, vocabulary=vocabulary
        )
        entry = {"tool": tool_name, "arguments": logged}
        self.activity.append(account_id, entry)
        if self.store is not None:
            self.store.append_activity(account_id, entry)
        if tool_name == "browser_open" and _PLACEHOLDER_ONLY.fullmatch(str(arguments.get("url", "")).strip()):
            # The person named this site (NER tagged it); the browser may resolve a bare name to its website.
            raw["named_site"] = True
        if not confirmed and self.confirm_reason(account_id, conversation_id, tool_name, arguments):
            return {"status": "confirm", "tool": tool_name}
        try:
            outcome = capability.invoke(account_id, tool.name, raw)
        except Exception as exc:
            outcome = str(exc).strip() or "that action failed"
        rendered, _ = render_result(
            outcome,
            capability.fields,
            vault,
            vocabulary=vocabulary,
            ner=self.ner_for(account_id),
            for_cloud=True,
        )
        # Scan decoded JSON values before escaping can hide a mapped string.
        try:
            structured = json.loads(rendered)
        except json.JSONDecodeError:
            structured = None
        if isinstance(structured, (dict, list)):
            rendered = json.dumps(_redact_value(structured, vault, vocabulary), sort_keys=True)
        text, _ = redact(rendered, vault, vocabulary=vocabulary)
        if self.store is not None:
            self.store.save_vault(vault)
        if for_model or tool.effect is Effect.EXTERNAL:
            return {"status": "done", "result": text}
        return {"status": "done", "result": vault.restore(text)}

    def confirm_reason(
        self,
        account_id: str,
        conversation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Why this call waits for the person, or empty when it can run now."""
        try:
            _capability, tool = self.registry.resolve(account_id, tool_name)
        except KeyError:
            return ""
        vault = self.vaults.get(account_id, conversation_id)
        if tool.egress and _egress_needs_restore(
            arguments, vault, approved=self._egress_approved(account_id)
        ):
            return "egress"
        if tool.effect is Effect.EXTERNAL or tool.confirm:
            return "external"
        if tool_name in {"lesson_save", "lesson_update", "memory_remember"} and _lesson_needs_confirm():
            return "lesson"
        return ""

    def _restore_store(self) -> None:
        assert self.store is not None
        self.vocabulary.update(self.store.load_vocabulary())
        for account_id in self.store.accounts():
            for entry in self.store.load_activity(account_id):
                self.activity.append(account_id, entry)
        for vault in self.store.load_vaults():
            self.vaults.put(vault)
        for account_id, conversation_id, record in self.store.load_traces():
            self._traces[(account_id, conversation_id)] = record
        for account_id, name, value in self.store.load_secrets():
            self.broker._secrets[(account_id, name)] = value
        self.schedules.update(self.store.load_schedules())


_PLACEHOLDER_ONLY = REFERENCE


def _egress_needs_restore(
    arguments: dict[str, Any], vault: Vault, *, approved: frozenset[str] = frozenset()
) -> bool:
    """True when an egress argument still holds a placeholder the person must approve."""
    return _arg_needs_restore(arguments, vault, approved)


def _arg_needs_restore(value: Any, vault: Vault, approved: frozenset[str] = frozenset()) -> bool:
    if isinstance(value, str):
        if "[" not in value or "]" not in value:
            return False
        for match in REFERENCE.finditer(vault.canonicalize(value)):
            restored = vault.restore(match.group())
            if restored != match.group() and restored not in approved:
                return True
        return False
    if isinstance(value, list):
        return any(_arg_needs_restore(item, vault, approved) for item in value)
    if isinstance(value, dict):
        return any(
            _arg_needs_restore(str(key), vault, approved) or _arg_needs_restore(item, vault, approved)
            for key, item in value.items()
        )
    return False


def _restore_arg(value: Any, vault: Vault) -> Any:
    if isinstance(value, str):
        return vault.restore(value, strict=True)
    if isinstance(value, list):
        return [_restore_arg(item, vault) for item in value]
    if isinstance(value, dict):
        restored: dict[str, Any] = {}
        for key, item in value.items():
            name = vault.restore(str(key), strict=True)
            if name in restored:
                raise ReferenceError("Action blocked: restored argument keys conflict.")
            restored[name] = _restore_arg(item, vault)
        return restored
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return vault.restore(str(value), strict=True)


def _redact_value(value: Any, vault: Vault, vocabulary: tuple[VocabularyTerm, ...]) -> Any:
    if isinstance(value, str):
        return redact(value, vault, vocabulary=vocabulary)[0]
    if isinstance(value, list):
        return [_redact_value(item, vault, vocabulary) for item in value]
    if isinstance(value, dict):
        return {
            redact(str(key), vault, vocabulary=vocabulary)[0]: _redact_value(item, vault, vocabulary)
            for key, item in value.items()
        }
    return value


def _lesson_needs_confirm() -> bool:
    """Lesson writes from a turn that saw untrusted tool results need a confirm."""
    turn = current_task.get()
    if turn is None:
        return False
    if turn.learning_source != "person":
        return False
    return bool(turn.tainted)
