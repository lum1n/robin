"""Deterministic detection, redaction, and restore.

This is the guarantee. A neural pass can add spans. It cannot weaken these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from robin.vault import REFERENCE, REFERENCE_CANDIDATE, Vault

REDACTED = "[REDACTED]"
UNRESOLVED = "[UNRESOLVED]"

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b"),
    re.compile(
        r"\b(?=[A-Za-z0-9+/]{32,}\b)(?=[A-Za-z0-9+/]*[a-z])"
        r"(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{32,}\b"
    ),
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = (
    re.compile(r"(?<!\w)\+\d{8,15}(?!\w)"),
    re.compile(r"(?<!\d)\+47[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{2}(?!\d)"),
    re.compile(r"(?<!\d)\b[49]\d{7}\b"),
)
_ELEVEN = re.compile(r"(?<!\d)\d{11}(?!\d)")
_NINE = re.compile(r"(?<!\d)\d{9}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")

_PRIORITY = {
    "SECRET": 0,
    "NATIONAL_ID": 1,
    "PAYMENT": 2,
    "EMAIL": 3,
    "PHONE": 4,
    "ADDRESS": 5,
    "PERSON": 6,
    "ORG": 7,
    "TEXT": 8,
}


class Disposition(Enum):
    DROP = "drop"
    TOKENIZE = "tokenize"


_DISPOSITION = {
    "SECRET": Disposition.DROP,
    "NATIONAL_ID": Disposition.DROP,
    "PAYMENT": Disposition.DROP,
    "EMAIL": Disposition.TOKENIZE,
    "PHONE": Disposition.TOKENIZE,
    "ADDRESS": Disposition.TOKENIZE,
    "PERSON": Disposition.TOKENIZE,
    "ORG": Disposition.TOKENIZE,
    "TEXT": Disposition.TOKENIZE,
}


@dataclass(frozen=True)
class VocabularyTerm:
    text: str
    label: str = "PERSON"


@dataclass(frozen=True)
class Entity:
    start: int
    end: int
    label: str
    # Spelling to store in the vault, so case variants of a known term share one reference.
    canonical: str = field(default="", compare=False)

    @property
    def disposition(self) -> Disposition:
        return _DISPOSITION[self.label]


@dataclass(frozen=True)
class Report:
    entities: tuple[Entity, ...]
    unresolved: bool

    @property
    def has_critical(self) -> bool:
        return any(entity.disposition is Disposition.DROP for entity in self.entities)

    def merge(self, other: Report) -> Report:
        return Report(
            entities=self.entities + other.entities,
            unresolved=self.unresolved or other.unresolved,
        )


def fodselsnummer_ok(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit():
        return False
    first = _mod11(digits[:9], (3, 7, 6, 1, 8, 9, 4, 5, 2))
    if first is None or first != int(digits[9]):
        return False
    second = _mod11(digits[:10], (5, 4, 3, 2, 7, 6, 5, 4, 3, 2))
    return second is not None and second == int(digits[10])


def kontonummer_ok(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit():
        return False
    check = _mod11(digits[:10], (5, 4, 3, 2, 7, 6, 5, 4, 3, 2))
    return check is not None and check == int(digits[10])


def organisasjonsnummer_ok(digits: str) -> bool:
    if len(digits) != 9 or not digits.isdigit():
        return False
    check = _mod11(digits[:8], (3, 2, 7, 6, 5, 4, 3, 2))
    return check is not None and check == int(digits[8])


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    compact = value.replace(" ", "")
    if not 15 <= len(compact) <= 34 or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    moved = compact[4:] + compact[:4]
    converted: list[str] = []
    for char in moved:
        if char.isdigit():
            converted.append(char)
        elif char.isalpha():
            converted.append(str(ord(char.upper()) - 55))
        else:
            return False
    return int("".join(converted)) % 97 == 1


def _mod11(digits: str, weights: tuple[int, ...]) -> int | None:
    total = sum(int(digit) * weight for digit, weight in zip(digits, weights, strict=True))
    remainder = total % 11
    check = 0 if remainder == 0 else 11 - remainder
    if check == 10:
        return None
    return check


def detect(text: str, vocabulary: tuple[VocabularyTerm, ...] = ()) -> tuple[Entity, ...]:
    found: list[Entity] = []
    for pattern in _SECRET_PATTERNS:
        found.extend(Entity(match.start(), match.end(), "SECRET") for match in pattern.finditer(text))
    for match in _ELEVEN.finditer(text):
        digits = match.group()
        if fodselsnummer_ok(digits):
            found.append(Entity(match.start(), match.end(), "NATIONAL_ID"))
        elif kontonummer_ok(digits):
            found.append(Entity(match.start(), match.end(), "PAYMENT"))
    for match in _NINE.finditer(text):
        if organisasjonsnummer_ok(match.group()):
            found.append(Entity(match.start(), match.end(), "PAYMENT"))
    for match in _CARD.finditer(text):
        digits = re.sub(r"[ -]", "", match.group())
        if luhn_ok(digits):
            found.append(Entity(match.start(), match.end(), "PAYMENT"))
    for match in _IBAN.finditer(text):
        if iban_ok(match.group()):
            found.append(Entity(match.start(), match.end(), "PAYMENT"))
    found.extend(Entity(match.start(), match.end(), "EMAIL") for match in _EMAIL.finditer(text))
    for pattern in _PHONE:
        found.extend(Entity(match.start(), match.end(), "PHONE") for match in pattern.finditer(text))
    for term in vocabulary:
        if not term.text:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(term.text) + r"(?!\w)", re.IGNORECASE)
        found.extend(
            Entity(match.start(), match.end(), term.label, term.text) for match in pattern.finditer(text)
        )
    return tuple(_select(found))


def redact(
    text: str,
    vault: Vault,
    *,
    vocabulary: tuple[VocabularyTerm, ...] = (),
    free_text: bool = False,
    ner_available: bool = False,
    extra: tuple[Entity, ...] = (),
) -> tuple[str, Report]:
    protected = [
        (match.start(), match.end()) for match in REFERENCE.finditer(text)
        if vault.has_reference(match.group())
    ]
    invalid = [
        Entity(match.start(), match.end(), "SECRET")
        for match in REFERENCE_CANDIDATE.finditer(text)
        if not vault.has_reference(match.group())
    ]
    known = [
        Entity(start, end, label) for start, end, label in vault.spans(text)
        if not any(start >= left and end <= right for left, right in protected)
    ]
    known_spans = {(entity.start, entity.end) for entity in known}
    detected = [
        entity for entity in [*detect(text, vocabulary), *extra, *invalid]
        if not any(entity.start < end and entity.end > start for start, end in protected)
        and (entity.disposition is Disposition.DROP or (entity.start, entity.end) not in known_spans)
    ]
    entities = tuple(_select([*detected, *known]))
    pieces: list[str] = []
    cursor = 0
    for entity in entities:
        pieces.append(text[cursor : entity.start])
        value = text[entity.start : entity.end]
        if entity.disposition is Disposition.DROP:
            pieces.append(REDACTED)
        else:
            pieces.append(vault.token(entity.label, entity.canonical or value))
        cursor = entity.end
    pieces.append(text[cursor:])
    return vault.canonicalize("".join(pieces)), Report(
        entities=entities, unresolved=free_text and not ner_available
    )


def release(
    text: str,
    vault: Vault,
    *,
    vocabulary: tuple[VocabularyTerm, ...] = (),
    free_text: bool = False,
    ner_available: bool = False,
    extra: tuple[Entity, ...] = (),
) -> str:
    """The only form of a string that may be sent to a model."""
    if not text:
        return ""
    redacted, report = redact(
        text,
        vault,
        vocabulary=vocabulary,
        free_text=free_text,
        ner_available=ner_available,
        extra=extra,
    )
    if free_text and report.unresolved:
        return UNRESOLVED
    return redacted


def _select(entities: list[Entity]) -> list[Entity]:
    ordered = sorted(
        entities,
        key=lambda entity: (
            _PRIORITY.get(entity.label, 9),
            -(entity.end - entity.start),
            entity.start,
        ),
    )
    chosen: list[Entity] = []
    occupied: list[tuple[int, int]] = []
    for entity in ordered:
        if entity.end <= entity.start:
            continue
        if any(entity.start < end and entity.end > start for start, end in occupied):
            continue
        chosen.append(entity)
        occupied.append((entity.start, entity.end))
    return sorted(chosen, key=lambda entity: entity.start)
