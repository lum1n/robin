"""Capability protocol and registry. The core has no domain knowledge."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from robin.airlock import REDACTED, UNRESOLVED, Entity, Report, VocabularyTerm, redact
from robin.ner import Ner, UnavailableNer
from robin.vault import Vault


class Effect(Enum):
    READ = "read"
    MUTATE = "mutate"
    EXTERNAL = "external"


class FieldClass(Enum):
    DROP = "drop"
    TOKENIZE = "tokenize"
    ORDINARY = "ordinary"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    effect: Effect
    drop_arguments: tuple[str, ...] = ()
    egress: bool = False
    confirm: bool = False
    untrusted: bool = False


@dataclass(frozen=True)
class FieldSpec:
    name: str
    klass: FieldClass
    label: str = "TEXT"
    free_text: bool = False


@dataclass(frozen=True)
class Result:
    """A tool result. Prefer records so the airlock can redact field by field."""

    text: str = ""
    records: list[dict[str, str]] | None = None


@dataclass
class DueWork:
    account_id: str
    conversation_id: str
    text: str
    finish: Callable[[str], None]


@dataclass
class ActiveTurn:
    account_id: str
    conversation_id: str
    allow_cloud: bool = False
    free_text: bool = False
    text: str = ""
    tainted: bool = False
    learning_source: str = "person"


current_task: ContextVar[ActiveTurn | None] = ContextVar("robin_task", default=None)


@dataclass(frozen=True)
class SecretAccepted:
    reply: str = ""
    resume: str = ""
    allow_cloud: bool = False
    free_text: bool = False


_INPUT_KINDS = frozenset({"text", "secret", "url", "email", "username", "otp", "number", "choice"})


@dataclass(frozen=True)
class InputField:
    id: str
    label: str
    kind: str = "text"
    required: bool = True
    placeholder: str = ""
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _INPUT_KINDS:
            raise ValueError(f"unknown input kind {self.kind!r}")


@dataclass(frozen=True)
class InputRequest:
    request_id: str
    title: str
    reason: str
    fields: tuple[InputField, ...]
    owner: str

    def public(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "title": self.title,
            "reason": self.reason,
            "owner": self.owner,
            "fields": [
                {
                    "id": field.id,
                    "label": field.label,
                    "kind": field.kind,
                    "required": field.required,
                    "placeholder": field.placeholder,
                    "options": list(field.options),
                }
                for field in self.fields
            ],
        }


class Capability:
    id: str
    tools: list[Tool]
    fields: list[FieldSpec]

    def visible_to(self, account_id: str) -> bool:
        return True

    def available_tools(self, account_id: str) -> list[Tool]:
        """Tools this account may call. May depend on state, never on request text."""
        return list(self.tools)

    def status(self, account_id: str) -> str:
        """One-line connector status for the system prompt, or empty."""
        return ""

    def guidance(self, account_id: str, text: str) -> list[str]:
        """Short lines for the system prompt about this account's learned preferences."""
        return []

    def accept_secret(self, account_id: str, conversation_id: str, text: str) -> SecretAccepted | None:
        return None

    def peel_secret(self, account_id: str, text: str) -> str | None:
        return None

    def pending_input(self, account_id: str, conversation_id: str) -> InputRequest | None:
        return None

    def accept_input(
        self,
        account_id: str,
        conversation_id: str,
        request_id: str,
        values: dict[str, str],
        *,
        cancel: bool = False,
    ) -> SecretAccepted | None:
        return None

    def due(self, now: datetime) -> list[DueWork]:
        return []

    def records(self, account_id: str) -> list[dict[str, str]]:
        return []

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        raise NotImplementedError(tool_name)


@dataclass
class Registry:
    _capabilities: list[Capability] = field(default_factory=list)
    _names: dict[str, str] = field(default_factory=dict)

    def add(self, capability: Capability) -> None:
        for tool in capability.tools:
            prior = self._names.get(tool.name)
            if prior is not None:
                raise ValueError(f"duplicate tool name {tool.name!r} from {capability.id!r} and {prior!r}")
            self._names[tool.name] = capability.id
        self._capabilities.append(capability)

    def claim_names(self, capability_id: str, names: list[str]) -> None:
        """Register dynamic tool names (for example MCP). Refuses collisions."""
        for name in names:
            prior = self._names.get(name)
            if prior is not None and prior != capability_id:
                raise ValueError(f"duplicate tool name {name!r} from {capability_id!r} and {prior!r}")
            self._names[name] = capability_id

    def release_names(self, capability_id: str, names: list[str]) -> None:
        for name in names:
            if self._names.get(name) == capability_id:
                del self._names[name]

    def for_account(self, account_id: str) -> list[Capability]:
        return [capability for capability in self._capabilities if capability.visible_to(account_id)]

    def resolve(self, account_id: str, tool_name: str) -> tuple[Capability, Tool]:
        for capability in self.for_account(account_id):
            for tool in capability.available_tools(account_id):
                if tool.name == tool_name:
                    return capability, tool
        raise KeyError(tool_name)

    def schemas(self, account_id: str) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for capability in self.for_account(account_id):
            for tool in capability.available_tools(account_id):
                schemas.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "effect": tool.effect.value,
                        "egress": tool.egress,
                    }
                )
        return schemas

    def statuses(self, account_id: str) -> list[str]:
        lines: list[str] = []
        for capability in self.for_account(account_id):
            line = capability.status(account_id).strip()
            if line:
                lines.append(line)
        return lines

    def guidance(self, account_id: str, text: str) -> list[str]:
        lines: list[str] = []
        for capability in self.for_account(account_id):
            lines.extend(capability.guidance(account_id, text))
        return lines

    def pending_input(self, account_id: str, conversation_id: str) -> InputRequest | None:
        for capability in self.for_account(account_id):
            found = capability.pending_input(account_id, conversation_id)
            if found is not None:
                return found
        return None

    def accept_input(
        self,
        account_id: str,
        conversation_id: str,
        request_id: str,
        values: dict[str, str],
        *,
        cancel: bool = False,
    ) -> SecretAccepted | None:
        for capability in self.for_account(account_id):
            accepted = capability.accept_input(
                account_id, conversation_id, request_id, values, cancel=cancel
            )
            if accepted is not None:
                return accepted
        return None


def render_records(
    records: list[dict[str, str]],
    fields: list[FieldSpec],
    vault: Vault,
    *,
    vocabulary: tuple[VocabularyTerm, ...] = (),
    ner: Ner | None = None,
    for_cloud: bool = True,
) -> tuple[list[dict[str, str]], Report]:
    detector = ner or UnavailableNer()
    rendered: list[dict[str, str]] = []
    report = Report(entities=(), unresolved=False)
    for record in records:
        row: dict[str, str] = {}
        for spec in fields:
            value = str(record.get(spec.name, ""))
            text, field_report = _render_field(
                value,
                spec,
                vault,
                vocabulary=vocabulary,
                ner=detector,
                for_cloud=for_cloud,
            )
            row[spec.name] = text
            report = report.merge(field_report)
        rendered.append(row)
    return rendered, report


def render_result(
    result: str | Result,
    fields: list[FieldSpec],
    vault: Vault,
    *,
    vocabulary: tuple[VocabularyTerm, ...] = (),
    ner: Ner | None = None,
    for_cloud: bool = True,
) -> tuple[str, Report]:
    """Release a tool result for the model. Records go through FieldSpecs."""
    if isinstance(result, Result) and result.records is not None:
        rows, report = render_records(
            result.records,
            fields,
            vault,
            vocabulary=vocabulary,
            ner=ner,
            for_cloud=for_cloud,
        )
        if result.text:
            return f"{result.text}\n{json.dumps(rows, sort_keys=True)}", report
        return json.dumps(rows, sort_keys=True), report
    text = result.text if isinstance(result, Result) else str(result)
    return text, Report(entities=(), unresolved=False)


def render_context(
    capabilities: list[Capability],
    account_id: str,
    vault: Vault,
    *,
    vocabulary: tuple[VocabularyTerm, ...] = (),
    ner: Ner | None = None,
    for_cloud: bool = True,
) -> tuple[str, Report]:
    chunks: list[dict[str, object]] = []
    report = Report(entities=(), unresolved=False)
    for capability in capabilities:
        records, field_report = render_records(
            capability.records(account_id),
            capability.fields,
            vault,
            vocabulary=vocabulary,
            ner=ner,
            for_cloud=for_cloud,
        )
        chunks.append({"id": capability.id, "records": records})
        report = report.merge(field_report)
    return json.dumps(chunks, sort_keys=True), report


def _render_field(
    value: str,
    spec: FieldSpec,
    vault: Vault,
    *,
    vocabulary: tuple[VocabularyTerm, ...],
    ner: Ner,
    for_cloud: bool,
) -> tuple[str, Report]:
    if spec.klass is FieldClass.DROP:
        if not value:
            return "", Report(entities=(), unresolved=False)
        return REDACTED, Report(entities=(Entity(0, len(value), "SECRET"),), unresolved=False)

    extra = ner.detect(value) if ner.available() else ()
    redacted, report = redact(
        value,
        vault,
        vocabulary=vocabulary,
        free_text=spec.free_text,
        ner_available=ner.available(),
        extra=extra,
    )
    if spec.klass is FieldClass.TOKENIZE and not spec.free_text and not report.has_critical and redacted == value and value:
        redacted = vault.token(spec.label, value)
        report = Report(entities=(Entity(0, len(value), spec.label),), unresolved=False)
    if for_cloud and spec.free_text and report.unresolved:
        return UNRESOLVED, report
    if for_cloud:
        return redacted, report
    return vault.restore(redacted), report
