"""Capability protocol and registry. The core has no domain knowledge."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class FieldSpec:
    name: str
    klass: FieldClass
    label: str = "TEXT"
    free_text: bool = False


class Capability:
    id: str
    tools: list[Tool]
    fields: list[FieldSpec]

    def visible_to(self, account_id: str) -> bool:
        return True

    def records(self, account_id: str) -> list[dict[str, str]]:
        return []

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        raise NotImplementedError(tool_name)


@dataclass
class Registry:
    _capabilities: list[Capability] = field(default_factory=list)

    def add(self, capability: Capability) -> None:
        self._capabilities.append(capability)

    def for_account(self, account_id: str) -> list[Capability]:
        return [capability for capability in self._capabilities if capability.visible_to(account_id)]

    def resolve(self, account_id: str, tool_name: str) -> tuple[Capability, Tool]:
        for capability in self.for_account(account_id):
            for tool in capability.tools:
                if tool.name == tool_name:
                    return capability, tool
        raise KeyError(tool_name)

    def schemas(self, account_id: str) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for capability in self.for_account(account_id):
            for tool in capability.tools:
                schemas.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "effect": tool.effect.value,
                    }
                )
        return schemas


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
