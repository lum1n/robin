"""Per-account lessons. Stored encrypted; released through the airlock."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from robin.airlock import redact
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool, current_task
from robin.store import HouseholdStore
from robin.vault import Vault

_GUIDANCE_CHARS = 1500
_KINDS = frozenset({"preference", "correction", "fact"})
_SITE = re.compile(r"(?<![\w@.-])(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])", re.IGNORECASE)


def _sites(text: str) -> list[str]:
    return [match.group(1).casefold() for match in _SITE.finditer(text)]


def _site_named(site: str, haystack: str) -> bool:
    """finn.no matches "finn.no" or the bare brand "finn" in the task text."""
    if site in haystack:
        return True
    stem = site.split(".", 1)[0]
    return len(stem) >= 3 and re.search(r"(?<!\w)" + re.escape(stem) + r"(?!\w)", haystack) is not None


def _relevance(row: dict[str, Any], haystack: str) -> int:
    """Word and tag overlap with the current request; hit count is a later tie-break."""
    if not haystack:
        return 0
    score = 0
    text = str(row.get("text") or "").casefold()
    words = {token for token in re.findall(r"[a-z0-9]{3,}", haystack)}
    fact_words = {token for token in re.findall(r"[a-z0-9]{3,}", text)}
    score += 3 * len(words & fact_words)
    for tag in row.get("tags") or []:
        folded = str(tag).casefold()
        if folded and folded in haystack:
            score += 4
        stem = folded.split(".", 1)[0]
        if len(stem) >= 3 and re.search(r"(?<!\w)" + re.escape(stem) + r"(?!\w)", haystack):
            score += 2
    return score


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_lesson(row: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    text = str(row.get("fact") or row.get("text") or "").strip()
    kind = str(row.get("kind") or "fact").strip().lower()
    if kind not in _KINDS:
        kind = "fact"
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    else:
        tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    return {
        "id": str(row.get("id") or fallback_id),
        "kind": kind,
        "text": text[:1000],
        "tags": tags,
        "source": str(row.get("source") or "person"),
        "created": str(row.get("created") or _now()),
        "used": str(row.get("used") or ""),
        "hits": int(row.get("hits") or 0),
    }


class Memory(Capability):
    id = "memory"
    tools = [
        Tool(
            name="lesson_save",
            description=(
                "Remember a lasting preference or correction for later turns. "
                "One imperative line. Optional tags (tool names or website hosts) scope when it applies."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["preference", "correction", "fact"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="lesson_update",
            description="Replace the text of a remembered lesson by id.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="lesson_list",
            description="List remembered lessons, optionally filtered by a query.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            effect=Effect.READ,
        ),
        Tool(
            name="lesson_forget",
            description="Forget a remembered lesson by id.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            effect=Effect.MUTATE,
        ),
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
        FieldSpec("kind", FieldClass.ORDINARY),
        FieldSpec("text", FieldClass.ORDINARY, free_text=True),
        FieldSpec("fact", FieldClass.ORDINARY, free_text=True),
        FieldSpec("tags", FieldClass.ORDINARY, free_text=True),
        FieldSpec("source", FieldClass.ORDINARY),
    ]

    def __init__(
        self,
        *,
        store: HouseholdStore | None = None,
        facts: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.store = store
        self._facts: dict[str, list[dict[str, Any]]] = {}
        raw = facts
        if store is not None and facts is None:
            raw = store.load_memory()
        for account, rows in (raw or {}).items():
            self._facts[account] = [
                _normalize_lesson(row, fallback_id=str(index + 1)) for index, row in enumerate(rows)
            ]

    def status(self, account_id: str) -> str:
        return f"memory: {len(self._facts.get(account_id, []))} lesson(s)"

    def guidance(self, account_id: str, text: str) -> list[str]:
        selected = self.select(account_id, text)
        if not selected:
            return []
        lines = ["Learned from this person (follow these over defaults; lessons beat skill steps when they conflict):"]
        for row in selected:
            kind = row.get("kind") or "fact"
            lines.append(f"- ({kind}) {row['text']}")
        return lines

    def select(self, account_id: str, text: str, *, hosts: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Pick lessons relevant to this task, capped for the system prompt."""
        haystack = " ".join((text or "", *hosts)).casefold()
        ranked: list[dict[str, Any]] = []
        for row in self._facts.get(account_id, []):
            tags = [str(tag) for tag in row.get("tags") or []]
            if tags and not any(tag.casefold() in haystack for tag in tags):
                continue
            # A lesson about one site (finn.no) must not steer tasks on another site.
            sites = _sites(str(row.get("text") or ""))
            if not tags and sites and not any(_site_named(site, haystack) for site in sites):
                continue
            ranked.append(row)
        ranked.sort(
            key=lambda row: (
                _relevance(row, haystack),
                int(row.get("hits") or 0),
                str(row.get("created") or ""),
            ),
            reverse=True,
        )
        chosen: list[dict[str, Any]] = []
        size = 0
        for row in ranked:
            line = f"- ({row.get('kind') or 'fact'}) {row['text']}"
            if size + len(line) > _GUIDANCE_CHARS and chosen:
                break
            chosen.append(row)
            size += len(line) + 1
        return chosen

    def mark_used(self, account_id: str, lesson_ids: list[str]) -> None:
        if not lesson_ids:
            return
        wanted = set(lesson_ids)
        changed = False
        stamp = _now()
        for row in self._facts.get(account_id, []):
            if row["id"] in wanted:
                row["hits"] = int(row.get("hits") or 0) + 1
                row["used"] = stamp
                changed = True
        if changed:
            self._save(account_id)

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name in {"lesson_save", "memory_remember"}:
            return self._save_lesson(account_id, arguments, tool_name=tool_name)
        if tool_name == "lesson_update":
            return self._update_lesson(account_id, arguments)
        if tool_name in {"lesson_list", "memory_recall"}:
            return self._list_lessons(account_id, arguments)
        if tool_name in {"lesson_forget", "memory_forget"}:
            return self._forget_lesson(account_id, arguments)
        raise NotImplementedError(tool_name)

    def _save_lesson(self, account_id: str, arguments: dict[str, Any], *, tool_name: str) -> str:
        if tool_name == "memory_remember":
            text = str(arguments.get("fact", "")).strip()
            kind = "fact"
            tags: list[str] = []
        else:
            text = str(arguments.get("text", "")).strip()
            kind = str(arguments.get("kind") or "preference").strip().lower()
            if kind not in _KINDS:
                kind = "preference"
            raw_tags = arguments.get("tags") or []
            if isinstance(raw_tags, str):
                tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
            else:
                tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        if not text or "\n" in text:
            return "lesson must be one line"
        blocked = self._blocked_text(account_id, text)
        if blocked:
            return blocked
        turn = current_task.get()
        source = "person"
        if turn is not None and turn.learning_source:
            source = turn.learning_source
        rows = self._facts.setdefault(account_id, [])
        fact_id = str(max((int(row["id"]) for row in rows if str(row["id"]).isdigit()), default=0) + 1)
        rows.append(
            {
                "id": fact_id,
                "kind": kind,
                "text": text[:1000],
                "tags": tags,
                "source": source,
                "created": _now(),
                "used": "",
                "hits": 0,
            }
        )
        self._save(account_id)
        return f"remembered {fact_id}"

    def _update_lesson(self, account_id: str, arguments: dict[str, Any]) -> str:
        fact_id = str(arguments.get("id", ""))
        text = str(arguments.get("text", "")).strip()
        if not text or "\n" in text:
            return "lesson must be one line"
        blocked = self._blocked_text(account_id, text)
        if blocked:
            return blocked
        for row in self._facts.get(account_id, []):
            if row.get("id") == fact_id:
                row["text"] = text[:1000]
                self._save(account_id)
                return f"updated {fact_id}"
        return "not found"

    def _list_lessons(self, account_id: str, arguments: dict[str, Any]) -> str | Result:
        query = str(arguments.get("query", "")).casefold()
        rows = []
        for row in self._facts.get(account_id, []):
            blob = " ".join(
                [
                    str(row.get("text") or ""),
                    str(row.get("kind") or ""),
                    " ".join(str(tag) for tag in row.get("tags") or []),
                ]
            ).casefold()
            if query and query not in blob:
                continue
            rows.append(
                {
                    "id": row["id"],
                    "kind": row.get("kind") or "fact",
                    "text": row.get("text") or "",
                    "tags": ", ".join(str(tag) for tag in row.get("tags") or []),
                    "source": row.get("source") or "",
                }
            )
        if not rows:
            return "No memories matched."
        return Result(text="Lessons:", records=rows)

    def _forget_lesson(self, account_id: str, arguments: dict[str, Any]) -> str:
        fact_id = str(arguments.get("id", ""))
        before = self._facts.get(account_id, [])
        kept = [row for row in before if row.get("id") != fact_id]
        self._facts[account_id] = kept
        self._save(account_id)
        return "forgotten" if len(kept) != len(before) else "not found"

    def _blocked_text(self, account_id: str, text: str) -> str:
        _, report = redact(text, Vault(account_id, "memory"))
        if report.has_critical:
            return "refused: lesson must not contain secrets or national IDs"
        return ""

    def _save(self, account_id: str) -> None:
        if self.store is not None:
            self.store.save_memory(account_id, self._facts.get(account_id, []))
