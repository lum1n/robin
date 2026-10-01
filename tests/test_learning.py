"""Lessons, skills, browser traces, and reflection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from robin.capabilities.memory import Memory
from robin.capabilities.skills import Skills
from robin.capability import ActiveTurn, current_task
from robin.learning import (
    BrowserTrace,
    Learning,
    draft_site_skill,
    looks_like_correction,
    reflect,
    skill_hint_for_open,
    strip_url,
)
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.policy import Task
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


class Scripted:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.seen: list[tuple[list[dict], list[str]]] = []

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.seen.append((messages, [tool["name"] for tool in tools]))
        return self.turns.pop(0)


def _system(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def test_looks_like_correction() -> None:
    assert looks_like_correction("No, use the filter instead")
    assert looks_like_correction("neste gang bruk kategorien")
    assert not looks_like_correction("buy milk")


def test_lesson_appears_in_system_prompt_on_later_turn(tmp_path: Path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    memory = Memory(store=store)
    assistant = Assistant(store=store)
    assistant.add(memory)
    memory.invoke("ada", "lesson_save", {"text": "Prefer short grocery lists", "kind": "preference"})
    model = Scripted([ModelTurn("ok")])
    converse(assistant, Task("ada", "t", "what should I buy"), model)
    prompt = _system(model.seen[0][0])
    assert "Prefer short grocery lists" in prompt
    assert "Learned from this person" in prompt


def test_tagged_lesson_only_when_relevant(tmp_path: Path) -> None:
    memory = Memory(facts={"ada": []})
    memory.invoke(
        "ada",
        "lesson_save",
        {"text": "On finn always open filters first", "kind": "correction", "tags": ["finn.no"]},
    )
    assert memory.select("ada", "check the weather") == []
    selected = memory.select("ada", "search finn.no for bikes")
    assert len(selected) == 1
    assert "filters" in selected[0]["text"]


def test_untagged_site_lesson_only_for_that_site() -> None:
    memory = Memory(facts={"ada": []})
    memory.invoke("ada", "lesson_save", {"text": "Search car models by year on finn.no"})
    memory.invoke("ada", "lesson_save", {"text": "Always use the tavily mcp for web searches"})
    other = [row["text"] for row in memory.select("ada", "search prisjakt.no for a TV")]
    assert other == ["Always use the tavily mcp for web searches"]
    assert len(memory.select("ada", "find a Golf on finn")) == 2
    assert len(memory.select("ada", "search https://www.finn.no/bap for bikes")) == 2


def test_lesson_placeholders_share_the_conversation_vault() -> None:
    from robin.airlock import VocabularyTerm

    assistant = Assistant()
    assistant.add(Memory(facts={"ada": []}))
    assistant.set_vocabulary("ada", (VocabularyTerm("Jane Doe"), VocabularyTerm("Bob Berg")))
    assistant.registry._capabilities[-1].invoke("ada", "lesson_save", {"text": "Jane Doe likes short replies"})
    model = Scripted([ModelTurn("ok")])
    converse(assistant, Task("ada", "t", "ask Bob Berg, then Jane Doe"), model)
    messages = model.seen[0][0]
    vault = assistant.vaults.get("ada", "t")
    jane = vault.token("PERSON", "Jane Doe")
    assert f"{jane} likes short replies" in _system(messages)
    assert jane in messages[1]["content"]
    assert vault.restore(jane) == "Jane Doe"


def test_guidance_cap_prefers_hits(tmp_path: Path) -> None:
    rows = []
    for index in range(40):
        rows.append(
            {
                "id": str(index + 1),
                "kind": "preference",
                "text": f"Preference number {index} " + ("x" * 80),
                "tags": [],
                "source": "person",
                "created": f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                "used": "",
                "hits": index,
            }
        )
    memory = Memory(facts={"ada": rows})
    selected = memory.select("ada", "hello")
    assert selected
    assert selected[0]["hits"] == 39
    assert sum(len(row["text"]) for row in selected) <= 1600


def test_reflection_saves_lesson_without_tool_results(tmp_path: Path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    memory = Memory(store=store)
    skills = Skills(store=store, known_tools={"browser_open", "browser_click", "lesson_save"})
    assistant = Assistant(store=store)
    assistant.add(memory)
    assistant.add(skills)
    trace = BrowserTrace(correction=True)
    trace.calls.append({"name": "browser_open", "arguments": {"url": "https://finn.no"}})
    model = Scripted(
        [
            ModelTurn(
                "",
                (
                    ToolCall(
                        "lesson_save",
                        {"text": "On finn use category filters first", "kind": "correction", "tags": ["finn.no"]},
                    ),
                ),
            ),
            ModelTurn(""),
        ]
    )
    saved = reflect(
        assistant,
        model,
        account_id="ada",
        conversation_id="t",
        person_text="No, use the category filter instead",
        reply_text="I searched.",
        trace=trace,
    )
    assert any("remembered" in line for line in saved)
    prompt_blob = str(model.seen[0][0])
    assert "Tool calls (no results)" in prompt_blob or "tool_calls" in prompt_blob.lower() or "browser_open" in prompt_blob
    assert "Interactive:" not in prompt_blob
    assert memory._facts["ada"][0]["text"].startswith("On finn")


def test_tainted_lesson_save_requires_confirm(tmp_path: Path) -> None:
    assistant = Assistant()
    assistant.add(Memory())
    token = current_task.set(ActiveTurn("ada", "t", text="search", tainted=True, learning_source="person"))
    try:
        outcome = assistant.invoke(
            "ada",
            "t",
            "lesson_save",
            {"text": "Always use filters", "kind": "preference"},
            for_model=True,
        )
    finally:
        current_task.reset(token)
    assert outcome["status"] == "confirm"


def test_reflection_lesson_skips_confirm_when_tainted_context_absent(tmp_path: Path) -> None:
    assistant = Assistant()
    memory = Memory()
    assistant.add(memory)
    token = current_task.set(
        ActiveTurn("ada", "t", text="search", tainted=False, learning_source="reflection")
    )
    try:
        outcome = assistant.invoke(
            "ada",
            "t",
            "lesson_save",
            {"text": "Always use filters", "kind": "preference", "tags": ["finn.no"]},
            for_model=True,
        )
    finally:
        current_task.reset(token)
    assert outcome["status"] == "done"
    assert "remembered" in outcome["result"]


def test_skill_propose_activate_and_read(tmp_path: Path) -> None:
    skills = Skills(known_tools={"browser_open", "browser_click", "browser_type"})
    assistant = Assistant()
    assistant.add(skills)
    proposed = skills.invoke(
        "ada",
        "skill_propose",
        {
            "name": "finn.no: search bikes",
            "description": "Find used bikes on finn",
            "steps": "1. browser_open https://finn.no\n2. browser_click button \"Filters\"",
            "tags": ["finn.no", "browser"],
        },
    )
    assert "proposed" in proposed
    held = assistant.invoke("ada", "t", "skill_activate", {"name": "finn.no: search bikes"}, for_model=True)
    assert held["status"] == "confirm"
    done = assistant.invoke(
        "ada",
        "t",
        "skill_activate",
        {"name": "finn.no: search bikes"},
        confirmed=True,
        for_model=True,
    )
    assert "activated" in done["result"]
    body = skills.invoke("ada", "skill_read", {"name": "finn.no: search bikes"})
    assert "browser_open" in body
    assert "Filters" in body


def test_browser_trace_resolves_refs_and_strips_query() -> None:
    trace = BrowserTrace()
    snapshot = (
        "URL: https://finn.no/search?q=bike#top\n"
        "Interactive:\n"
        '[3] button "Filters"\n'
        '[4] textbox "Search"\n'
        "Content:\n"
        "results"
    )
    trace.note_snapshot(snapshot)
    trace.note_call("browser_open", {"url": "https://finn.no/search?q=bike#top"}, snapshot, failed=False)
    trace.note_call("browser_click", {"target": "3"}, snapshot, failed=False)
    trace.note_call("browser_type", {"target": "4", "text": "secret password"}, snapshot, failed=False)
    trace.note_call("browser_click", {"target": "3"}, "could not click", failed=True)
    steps = trace.cleaned_steps()
    assert strip_url("https://finn.no/search?q=bike#top") == "https://finn.no/search"
    assert steps[0] == "open https://finn.no/search"
    assert 'click button "Filters"' in steps[1]
    assert "<text from request>" in steps[2]
    assert "secret password" not in "\n".join(steps)
    assert len(steps) == 3


def test_successful_browser_turn_drafts_site_skill(tmp_path: Path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    skills = Skills(store=store, known_tools={"browser_open", "browser_click", "browser_type", "browser_press"})
    assistant = Assistant(store=store)
    assistant.add(Memory(store=store))
    assistant.add(skills)
    trace = BrowserTrace()
    snap = 'URL: https://finn.no/\nInteractive:\n[1] button "Søk"\nContent:\nok'
    for name, args in (
        ("browser_open", {"url": "https://finn.no"}),
        ("browser_click", {"target": "1"}),
        ("browser_type", {"target": "1", "text": "bike"}),
        ("browser_press", {"key": "Enter"}),
    ):
        if name == "browser_open":
            trace.note_snapshot(snap)
        trace.note_call(name, args, snap, failed=False)
    assert trace.should_draft_site_skill(reply_status="reply", reply_text="Found bikes.")
    result = draft_site_skill(assistant, "ada", "t", "find bikes on finn.no", trace)
    assert "proposed" in result
    drafts = skills.for_host("ada", "finn.no", status="proposed")
    assert drafts
    assert drafts[0]["name"].startswith("finn.no:")


def test_skill_hint_on_host_with_active_skill(tmp_path: Path) -> None:
    skills = Skills(known_tools={"browser_open", "browser_click"})
    assistant = Assistant()
    assistant.add(skills)
    skills.propose(
        "ada",
        name="finn.no: search bikes",
        description="Find bikes",
        steps="1. browser_open https://finn.no\n2. browser_click button \"Søk\"",
        tags=["finn.no", "browser"],
    )
    assistant.invoke("ada", "t", "skill_activate", {"name": "finn.no: search bikes"}, confirmed=True)
    hint = skill_hint_for_open(assistant, "ada", "https://finn.no/search")
    assert "skill_read" in hint
    assert "finn.no: search bikes" in hint


def test_nightly_due_only_with_new_turns(tmp_path: Path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    store.ensure_account("ada")
    memory = Memory(store=store)
    skills = Skills(store=store)
    learning = Learning(store=store, memory=memory, skills=skills)
    assistant = Assistant(store=store)
    assistant.add(memory)
    assistant.add(skills)
    assistant.add(learning)
    night = datetime(2026, 3, 1, 3, 0, tzinfo=timezone.utc)
    assert learning.due(night) == []
    store.append_turn("ada", "chat", "user", "hello")
    due = learning.due(night)
    assert len(due) == 1
    assert due[0].conversation_id == "learning"
    due[0].finish("reviewed")
    assert learning.due(night) == []


def test_correction_turn_reflects_with_sync_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROBIN_REFLECT_SYNC", "1")
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    memory = Memory(store=store)
    skills = Skills(store=store, known_tools={"lesson_save", "browser_open"})
    assistant = Assistant(store=store)
    assistant.add(memory)
    assistant.add(skills)
    model = Scripted(
        [
            ModelTurn("I searched the page."),
            ModelTurn(
                "",
                (
                    ToolCall(
                        "lesson_save",
                        {
                            "text": "Prefer category filters on finn",
                            "kind": "correction",
                            "tags": ["finn.no"],
                        },
                    ),
                ),
            ),
            ModelTurn(""),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "No, use filters instead next time"), model)
    assert reply.status == "reply"
    assert memory._facts.get("ada")
    assert "Prefer category filters" in memory._facts["ada"][0]["text"]
