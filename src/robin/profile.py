"""Per-account personal details for form fill and for answering the person.

Raw values never go to the model. They become conversation-scoped references in the
prompt and are restored only in the person-facing reply and inside local tool arguments.
Secrets, national IDs, and payment data do not belong in this profile.
"""

from __future__ import annotations

import json
from typing import Any

from robin.airlock import VocabularyTerm
from robin.vault import Vault

PROFILE_SECRET = "profile"

# Stable keys the apps and browser_fill_profile share.
PROFILE_FIELDS = (
    "given_name",
    "family_name",
    "full_name",
    "email",
    "phone",
    "address",
    "city",
    "postal_code",
    "country",
)

_ALIASES = {
    "given_name": "given_name",
    "first_name": "given_name",
    "fornavn": "given_name",
    "family_name": "family_name",
    "last_name": "family_name",
    "surname": "family_name",
    "etternavn": "family_name",
    "full_name": "full_name",
    "name": "full_name",
    "navn": "full_name",
    "email": "email",
    "e-post": "email",
    "e_post": "email",
    "epost": "email",
    "phone": "phone",
    "telefon": "phone",
    "mobile": "phone",
    "mobil": "phone",
    "address": "address",
    "street": "address",
    "adresse": "address",
    "city": "city",
    "sted": "city",
    "postal_code": "postal_code",
    "postcode": "postal_code",
    "zip": "postal_code",
    "postnummer": "postal_code",
    "country": "country",
    "land": "country",
}


def normalize_field(name: str) -> str:
    key = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(key, "")


def empty_profile() -> dict[str, str]:
    return {key: "" for key in PROFILE_FIELDS}


def parse_profile(raw: str) -> dict[str, str]:
    out = empty_profile()
    if not raw or not raw.strip():
        return out
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return out
    if not isinstance(data, dict):
        return out
    for key in PROFILE_FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            out[key] = value.strip()
    return out


def dump_profile(fields: dict[str, str]) -> str:
    cleaned = empty_profile()
    for key in PROFILE_FIELDS:
        value = fields.get(key, "")
        cleaned[key] = value.strip() if isinstance(value, str) else ""
    return json.dumps(cleaned, sort_keys=True)


def merge_profile(current: dict[str, str], updates: dict[str, Any]) -> dict[str, str]:
    next_fields = dict(current)
    for key, value in updates.items():
        field = key if key in PROFILE_FIELDS else normalize_field(str(key))
        if field not in PROFILE_FIELDS:
            continue
        if value is None:
            continue
        if not isinstance(value, str):
            continue
        next_fields[field] = value.strip()
    if not next_fields.get("full_name"):
        parts = [next_fields.get("given_name", ""), next_fields.get("family_name", "")]
        composed = " ".join(part for part in parts if part).strip()
        if composed:
            next_fields["full_name"] = composed
    return next_fields


def profile_value(fields: dict[str, str], field: str) -> str:
    key = normalize_field(field) or (field if field in PROFILE_FIELDS else "")
    if not key:
        return ""
    value = (fields.get(key) or "").strip()
    if value:
        return value
    if key == "full_name":
        parts = [fields.get("given_name", ""), fields.get("family_name", "")]
        return " ".join(part for part in parts if part).strip()
    if key == "given_name" and fields.get("full_name"):
        return fields["full_name"].split()[0]
    if key == "family_name" and fields.get("full_name"):
        bits = fields["full_name"].split()
        return bits[-1] if len(bits) > 1 else ""
    return ""


def filled_keys(fields: dict[str, str]) -> list[str]:
    return [key for key in PROFILE_FIELDS if (fields.get(key) or "").strip()]


def secret_values(fields: dict[str, str]) -> list[str]:
    found = [value.strip() for value in fields.values() if isinstance(value, str) and value.strip()]
    return sorted(set(found), key=len, reverse=True)


_FIELD_LABELS = {
    "given_name": "PERSON",
    "family_name": "PERSON",
    "full_name": "PERSON",
    "email": "EMAIL",
    "phone": "PHONE",
    "address": "ADDRESS",
    "city": "ADDRESS",
    "postal_code": "ADDRESS",
    "country": "ADDRESS",
}

_FIELD_TITLES = {
    "given_name": "given name",
    "family_name": "family name",
    "full_name": "name",
    "email": "email",
    "phone": "phone",
    "address": "address",
    "city": "city",
    "postal_code": "postal code",
    "country": "country",
}


def profile_terms(fields: dict[str, str]) -> tuple[VocabularyTerm, ...]:
    """Longest values first, so a full address is one reference rather than its city alone."""
    found: list[VocabularyTerm] = []
    seen: set[str] = set()
    for key in PROFILE_FIELDS:
        value = (fields.get(key) or "").strip()
        folded = value.casefold()
        if not value or folded in seen:
            continue
        seen.add(folded)
        found.append(VocabularyTerm(value, _FIELD_LABELS[key]))
    found.sort(key=lambda term: len(term.text), reverse=True)
    return tuple(found)


def profile_line(fields: dict[str, str], vault: Vault | None) -> str:
    """Prompt line with typed references so the person can hear their own details."""
    if vault is None:
        return ""
    parts: list[str] = []
    for key in PROFILE_FIELDS:
        value = (fields.get(key) or "").strip()
        if not value:
            continue
        parts.append(f"{_FIELD_TITLES[key]} {vault.token(_FIELD_LABELS[key], value)}")
    if not parts:
        return ""
    return (
        "- Saved profile: "
        + "; ".join(parts)
        + ". When they ask for their own address or these details, copy the matching reference "
        "into the reply (it is shown to them). For website forms, use browser_fill_profile. "
        "Never invent these values."
    )
