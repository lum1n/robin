"""Household members and task preferences, stored encrypted per account.

Member names never reach the model: the prompt shows a relation, a PERSON reference, and a coarse
age band. The same names become vocabulary terms, so mentions in chat or tool results map to the
same references and are restored locally only inside tool arguments. Birth years stay local.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from robin.airlock import VocabularyTerm
from robin.vault import Vault

HOUSEHOLD_SECRET = "household"
PREFERENCES_SECRET = "preferences"

RELATIONS = ("partner", "child", "parent", "sibling", "other")
STYLES = ("auto", "concise", "detailed")
MAX_MEMBERS = 12
MAX_SOURCES = 12
_NAME_CHARS = (2, 60)
_TOPIC_CHARS = 30
_HOST = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_TOPIC = re.compile(r"[\w][\w &/+'-]{0,29}")


class HouseholdError(ValueError):
    pass


@dataclass(frozen=True)
class Member:
    relation: str
    given_name: str
    family_name: str = ""
    birth_year: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.given_name} {self.family_name}".strip() if self.family_name else ""


@dataclass(frozen=True)
class Source:
    topic: str
    host: str


@dataclass(frozen=True)
class Preferences:
    sources: tuple[Source, ...] = ()
    style: str = "auto"


def parse_members(raw: Any, *, current_year: int) -> tuple[Member, ...]:
    """Validate a member list from a client; raise HouseholdError on anything malformed."""
    if not isinstance(raw, list):
        raise HouseholdError("members must be a list")
    if len(raw) > MAX_MEMBERS:
        raise HouseholdError(f"at most {MAX_MEMBERS} members")
    members: list[Member] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HouseholdError("each member must be an object")
        relation = item.get("relation")
        if relation not in RELATIONS:
            raise HouseholdError(f"relation must be one of {', '.join(RELATIONS)}")
        given = _name(item.get("given_name"), required=True)
        family = _name(item.get("family_name"), required=False)
        year = item.get("birth_year")
        if year is not None and (
            isinstance(year, bool) or not isinstance(year, int) or not current_year - 120 <= year <= current_year
        ):
            raise HouseholdError("birth_year must be a plausible year")
        members.append(Member(relation, given, family, year))
    return tuple(members)


def load_members(raw: str) -> tuple[Member, ...]:
    try:
        data = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return ()
    members: list[Member] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("relation") in RELATIONS and isinstance(item.get("given_name"), str):
            year = item.get("birth_year")
            members.append(
                Member(
                    item["relation"],
                    item["given_name"],
                    item.get("family_name") or "",
                    year if isinstance(year, int) and not isinstance(year, bool) else None,
                )
            )
    return tuple(members[:MAX_MEMBERS])


def dump_members(members: tuple[Member, ...]) -> str:
    return json.dumps([member_dict(member) for member in members], sort_keys=True)


def member_dict(member: Member) -> dict[str, Any]:
    return {
        "relation": member.relation,
        "given_name": member.given_name,
        "family_name": member.family_name,
        "birth_year": member.birth_year,
    }


def age_band(birth_year: int | None, current_year: int) -> str:
    if birth_year is None:
        return ""
    age = current_year - birth_year
    if age < 6:
        return "under 6"
    if age < 13:
        return "6-12"
    if age < 18:
        return "13-17"
    return "adult"


def member_terms(members: tuple[Member, ...]) -> tuple[VocabularyTerm, ...]:
    """Longest names first, so a full name is one reference rather than two."""
    names = {name for member in members for name in (member.full_name, member.given_name) if name}
    return tuple(VocabularyTerm(name, "PERSON") for name in sorted(names, key=len, reverse=True))


def household_line(members: tuple[Member, ...], vault: Vault | None, *, current_year: int) -> str:
    if not members or vault is None:
        return ""
    parts: list[str] = []
    for member in members:
        part = f"{member.relation} {vault.token('PERSON', member.given_name)}"
        if member.full_name:
            part += f" (full name {vault.token('PERSON', member.full_name)})"
        band = age_band(member.birth_year, current_year)
        if band:
            part += f", age {band}"
        parts.append(part)
    return (
        f"- Household: {'; '.join(parts)}. Resolve 'my wife/husband/partner', 'the kids', etc. to these references "
        "and copy them into tool arguments when needed; never guess or reveal the real names."
    )


def sources_line(preferences: Preferences) -> str:
    if not preferences.sources:
        return ""
    pairs = "; ".join(f"{source.topic} -> {source.host}" for source in preferences.sources)
    return (
        f"- Preferred sources: {pairs}. For those topics, try these sites first (e.g. web_search with "
        "'site:<host>' or browser_open), then fall back to others if they lack the answer."
    )


def parse_preferences(raw: Any) -> Preferences:
    if not isinstance(raw, dict):
        raise HouseholdError("preferences must be an object")
    style = raw.get("style", "auto")
    if style not in STYLES:
        raise HouseholdError(f"style must be one of {', '.join(STYLES)}")
    items = raw.get("sources", [])
    if not isinstance(items, list):
        raise HouseholdError("sources must be a list")
    if len(items) > MAX_SOURCES:
        raise HouseholdError(f"at most {MAX_SOURCES} sources")
    sources: list[Source] = []
    for item in items:
        if not isinstance(item, dict):
            raise HouseholdError("each source must be an object")
        topic = _clean(item.get("topic"))
        if not topic or len(topic) > _TOPIC_CHARS or not _TOPIC.fullmatch(topic):
            raise HouseholdError("topic must be 1-30 letters, digits, or spaces")
        host = _host(item.get("host"))
        if not host:
            raise HouseholdError("host must be a domain such as nrk.no")
        sources.append(Source(topic, host))
    return Preferences(tuple(sources), style)


def load_preferences(raw: str) -> Preferences:
    try:
        return parse_preferences(json.loads(raw) if raw else {})
    except (json.JSONDecodeError, HouseholdError):
        return Preferences()


def dump_preferences(preferences: Preferences) -> str:
    return json.dumps(preferences_dict(preferences), sort_keys=True)


def preferences_dict(preferences: Preferences) -> dict[str, Any]:
    return {
        "style": preferences.style,
        "sources": [{"topic": source.topic, "host": source.host} for source in preferences.sources],
    }


def _name(value: Any, *, required: bool) -> str:
    if value is None or value == "":
        if required:
            raise HouseholdError("given_name is required")
        return ""
    text = _clean(value)
    low, high = _NAME_CHARS
    # Brackets would let a name forge a reference.
    if not low <= len(text) <= high or any(char in text for char in "[]{}<>"):
        raise HouseholdError(f"names must be {low}-{high} characters without brackets")
    return text


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(char for char in value if unicodedata.category(char)[0] != "C")
    return " ".join(text.split())


def _host(value: Any) -> str:
    text = _clean(value).lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
    return text if _HOST.fullmatch(text) else ""
