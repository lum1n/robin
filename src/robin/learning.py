"""Turn reflection, browser traces, and nightly lesson review."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from robin.capability import ActiveTurn, Capability, DueWork, Effect, FieldClass, FieldSpec, Result, Tool, current_task
from robin.model import Model
from robin.store import HouseholdStore

_REF_LINE = re.compile(r'^\[(\d+)\]\s+(\w+)\s+"(.*)"(?:\s+\(([^)]*)\))?\s*$')
_FAILURE_LEAD = re.compile(
    r"^(no |could not |control \[|not (found|available)|the page did not|url must|Chromium)",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"(?i)(\bno[\s,]+|instead\b|not like that|next time|always\b|never\b|i prefer|"
    r"\bnei[\s,]+|\bheller\b|neste gang|\balltid\b|\baldri\b|husk at|ikke gjør|"
    r"do it (this|that) way|from now on)"
)
_BROWSER_TOOLS = frozenset(
    {
        "browser_open",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_scroll",
        "browser_press",
        "browser_hover",
        "browser_type_focused",
        "browser_back",
        "browser_switch",
        "browser_fill_username",
        "browser_fill_password",
        "browser_fill_profile",
        "browser_type_password",
        "browser_submit",
    }
)
_TRACE_TOOLS = _BROWSER_TOOLS - {"browser_read", "browser_fill_password", "browser_type_password", "browser_hover"}
_UNTRUSTED_PREFIXES = ("browser_", "mail_", "web_")
_UNTRUSTED_NAMES = frozenset({"files_list", "files_read", "files_search"})
_REFLECT_TOOLS = frozenset({"lesson_save", "lesson_update", "skill_propose"})
_REVIEW_HOUR = 3
_MIN_BROWSER_STEPS = 3


def is_untrusted_tool(name: str, *, untrusted: bool = False) -> bool:
    if untrusted:
        return True
    return name.startswith(_UNTRUSTED_PREFIXES) or name in _UNTRUSTED_NAMES


def looks_like_correction(text: str) -> bool:
    return bool(_CORRECTION.search(text or ""))


def is_tool_failure(result: str) -> bool:
    line = (result or "").strip().splitlines()[0].strip() if result else ""
    if not line:
        return False
    lower = line.lower()
    return bool(_FAILURE_LEAD.match(line)) or any(
        phrase in lower
        for phrase in (
            "not clickable",
            "gone or not clickable",
            "could not be clicked",
            "no clickable",
            "no text field",
            "no type=submit",
        )
    )


def strip_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlparse(raw)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    host = (parts.hostname or "").casefold().removeprefix("www.")
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not host:
        return path
    return f"https://{host}{path}"


def host_of(url: str) -> str:
    cleaned = strip_url(url)
    try:
        return (urlparse(cleaned).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


@dataclass
class TraceStep:
    tool: str
    line: str
    host: str = ""
    ok: bool = True


@dataclass
class BrowserTrace:
    steps: list[TraceStep] = field(default_factory=list)
    last_refs: dict[str, tuple[str, str]] = field(default_factory=dict)
    hosts: list[str] = field(default_factory=list)
    stuck: bool = False
    hit_max_steps: bool = False
    handoff: bool = False
    correction: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def note_snapshot(self, text: str) -> None:
        refs: dict[str, tuple[str, str]] = {}
        for raw in (text or "").splitlines():
            match = _REF_LINE.match(raw.strip())
            if match:
                refs[match.group(1)] = (match.group(2), match.group(3))
        if refs:
            self.last_refs = refs
        host = ""
        for raw in (text or "").splitlines():
            if raw.startswith("URL:"):
                host = host_of(raw[4:].strip())
                break
        if host and host not in self.hosts:
            self.hosts.append(host)

    def note_call(self, name: str, arguments: dict[str, Any], result: str, *, failed: bool) -> None:
        self.calls.append({"name": name, "arguments": dict(arguments)})
        if name not in _TRACE_TOOLS:
            if name.startswith("browser_"):
                self.note_snapshot(result)
            return
        if failed:
            return
        line = self._format_step(name, arguments, result)
        if not line:
            return
        host = ""
        if name == "browser_open":
            host = host_of(str(arguments.get("url") or ""))
        elif self.hosts:
            host = self.hosts[-1]
        if host and host not in self.hosts:
            self.hosts.append(host)
        self.steps.append(TraceStep(tool=name, line=line, host=host, ok=True))
        self.note_snapshot(result)

    def cleaned_steps(self) -> list[str]:
        return [step.line for step in self.steps if step.ok]

    def should_reflect(self, *, reply_status: str, reply_text: str) -> bool:
        if self.correction or self.stuck or self.hit_max_steps or self.handoff:
            return True
        if reply_status == "handoff":
            return True
        if (reply_text or "").strip() == "I could not finish that.":
            return True
        if reply_status == "reply" and len(self.cleaned_steps()) >= _MIN_BROWSER_STEPS:
            return True
        return False

    def should_draft_site_skill(self, *, reply_status: str, reply_text: str) -> bool:
        if reply_status != "reply":
            return False
        if self.stuck or self.hit_max_steps or self.handoff:
            return False
        if (reply_text or "").strip() == "I could not finish that.":
            return False
        return len(self.cleaned_steps()) >= _MIN_BROWSER_STEPS and bool(self.hosts)

    def _format_step(self, name: str, arguments: dict[str, Any], result: str) -> str:
        if name == "browser_open":
            return f'open {strip_url(str(arguments.get("url") or ""))}'
        if name == "browser_back":
            return "back"
        if name == "browser_submit":
            return "submit"
        if name == "browser_switch":
            return f"switch page {arguments.get('index') or arguments.get('target') or ''}".strip()
        if name == "browser_scroll":
            direction = str(arguments.get("direction") or "down")
            return f"scroll {direction}"
        if name == "browser_press":
            return f"press {arguments.get('key') or ''}".strip()
        if name == "browser_fill_profile":
            field_name = str(arguments.get("field") or "profile")
            target = self._target_label(str(arguments.get("target") or ""))
            return f'fill_profile {field_name} into {target}'.strip()
        if name == "browser_fill_username":
            target = self._target_label(str(arguments.get("target") or ""))
            return f"fill_username into {target}".strip()
        if name == "browser_type":
            target = self._target_label(str(arguments.get("target") or ""))
            return f'type <text from request> into {target}'.strip()
        if name == "browser_type_focused":
            return "type_focused <text from request>"
        if name == "browser_select":
            target = self._target_label(str(arguments.get("target") or ""))
            value = str(arguments.get("value") or "")
            return f'select "{value}" in {target}'.strip()
        if name == "browser_click":
            target = self._target_label(str(arguments.get("target") or ""))
            return f"click {target}".strip()
        return name

    def _target_label(self, target: str) -> str:
        raw = (target or "").strip()
        if not raw:
            return "control"
        if raw.isdigit() and raw in self.last_refs:
            role, name = self.last_refs[raw]
            return f'{role} "{name}"'
        if raw.startswith("[") and raw[1:].rstrip("]").isdigit():
            key = raw.strip("[]")
            if key in self.last_refs:
                role, name = self.last_refs[key]
                return f'{role} "{name}"'
        return f'"{raw}"'


def reflect(
    assistant: Any,
    model: Model,
    *,
    account_id: str,
    conversation_id: str,
    person_text: str,
    reply_text: str,
    trace: BrowserTrace,
    allow_cloud: bool = False,
    free_text: bool = False,
) -> list[str]:
    """One extra model call that may save lessons or propose skills. No tool result bodies."""
    tools = [
        tool
        for tool in assistant.tools(account_id)
        if tool.get("name") in _REFLECT_TOOLS
    ]
    if not tools:
        return []
    call_lines = []
    for call in trace.calls:
        call_lines.append(json.dumps({"name": call["name"], "arguments": call["arguments"]}, sort_keys=True))
    step_block = "\n".join(f"- {line}" for line in trace.cleaned_steps()) or "(none)"
    hosts = ", ".join(trace.hosts) or "(none)"
    prompt = (
        "Review this turn and learn lasting preferences for this person.\n"
        "Save a short imperative lesson when they corrected Robin or stated a lasting preference.\n"
        "If a multi-step browser flow worked, skill_propose a site skill named 'host: intent'.\n"
        "Do not invent secrets. Do not quote page content you were not given.\n"
        f"Hosts: {hosts}\n"
        f"Person: {person_text}\n"
        f"Robin reply: {reply_text}\n"
        f"Tool calls (no results):\n" + ("\n".join(call_lines) or "(none)") + "\n"
        f"Clean browser steps:\n{step_block}"
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are Robin's learning pass. Call lesson_save / lesson_update / skill_propose when useful. "
                "Otherwise answer with an empty message. Lessons are one line. Tag website hosts when relevant."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    token = current_task.set(
        _reflection_turn(account_id, conversation_id, allow_cloud, free_text, person_text)
    )
    saved: list[str] = []
    try:
        for _ in range(4):
            turn = model.complete(messages=messages, tools=tools)
            if not turn.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.message or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in turn.tool_calls
                    ],
                }
            )
            for call in turn.tool_calls:
                if call.name not in _REFLECT_TOOLS:
                    result = "tool not allowed in reflection"
                else:
                    outcome = assistant.invoke(
                        account_id,
                        conversation_id,
                        call.name,
                        call.arguments,
                        for_model=True,
                    )
                    result = str(outcome.get("result") or outcome.get("status") or "")
                    saved.append(f"{call.name}: {result}")
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result[:2000]})
    finally:
        current_task.reset(token)
    return saved


def _reflection_turn(
    account_id: str,
    conversation_id: str,
    allow_cloud: bool,
    free_text: bool,
    text: str,
) -> ActiveTurn:
    return ActiveTurn(
        account_id,
        conversation_id,
        allow_cloud,
        free_text,
        text,
        tainted=False,
        learning_source="reflection",
    )


def draft_site_skill(
    assistant: Any,
    account_id: str,
    conversation_id: str,
    person_text: str,
    trace: BrowserTrace,
) -> str:
    skills = _skills(assistant)
    if skills is None or not trace.hosts:
        return ""
    host = trace.hosts[0]
    intent = _intent_from(person_text, host)
    name = f"{host}: {intent}"[:200]
    steps = "\n".join(f"{index}. {line}" for index, line in enumerate(trace.cleaned_steps(), start=1))
    description = f"How this person completes '{intent}' on {host}."
    return skills.propose(
        account_id,
        name=name,
        description=description,
        steps=steps,
        tags=[host, "browser"],
        source_thread=conversation_id,
    )


def _draft_site_skill(
    assistant: Any,
    account_id: str,
    conversation_id: str,
    person_text: str,
    trace: BrowserTrace,
) -> str:
    result = draft_site_skill(assistant, account_id, conversation_id, person_text, trace)
    return f"skill_propose: {result}" if result else ""


def _intent_from(person_text: str, host: str) -> str:
    text = " ".join((person_text or "").split())
    lowered = text.casefold()
    for noise in (host, f"https://{host}", f"http://{host}", "www."):
        lowered = lowered.replace(noise, " ")
    cleaned = " ".join(lowered.split())[:80].strip(" .,:;")
    return cleaned or "site task"


def _skills(assistant: Any) -> Any:
    for capability in assistant.registry._capabilities:
        if getattr(capability, "id", "") == "skills":
            return capability
        if callable(getattr(capability, "for_host", None)) and callable(getattr(capability, "propose", None)):
            return capability
    return None


def skill_hint_for_open(assistant: Any, account_id: str, url: str) -> str:
    skills = _skills(assistant)
    if skills is None:
        return ""
    host = host_of(url)
    if not host:
        return ""
    matches = skills.for_host(account_id, host, status="active")
    if not matches:
        return ""
    names = ", ".join(row["name"] for row in matches[:3])
    return f"Robin has a skill for this site: {names} — skill_read it before acting."


def offer_line_for_draft(assistant: Any, account_id: str, host: str) -> str:
    skills = _skills(assistant)
    if skills is None or not host:
        return ""
    drafts = skills.for_host(account_id, host, status="proposed")
    if not drafts:
        return ""
    return f"I can remember how I did this on {host} for next time."


def schedule_reflect(
    assistant: Any,
    model: Model,
    *,
    account_id: str,
    conversation_id: str,
    person_text: str,
    reply_text: str,
    trace: BrowserTrace,
    allow_cloud: bool = False,
    free_text: bool = False,
    sync: bool = False,
) -> None:
    kwargs = dict(
        account_id=account_id,
        conversation_id=conversation_id,
        person_text=person_text,
        reply_text=reply_text,
        trace=trace,
        allow_cloud=allow_cloud,
        free_text=free_text,
    )
    if sync or os.environ.get("ROBIN_REFLECT_SYNC", "") in ("1", "true", "yes"):
        reflect(assistant, model, **kwargs)
        return

    def run() -> None:
        try:
            reflect(assistant, model, **kwargs)
        except Exception:
            return

    threading.Thread(target=run, name="robin-reflect", daemon=True).start()


REVIEW = (
    "Review this account's recent turns and lessons. "
    "Call learning_digest first. Merge duplicate lessons with lesson_update/lesson_forget. "
    "When the same multi-step browser flow on one host happened more than once, skill_propose a site skill. "
    "List proposed skills that still need skill_activate. Do not send, pay, or delete."
)


class Learning(Capability):
    id = "learning"
    tools = [
        Tool(
            name="learning_digest",
            description=(
                "Local digest of recent person lines, Robin replies, and tool-call names/hosts "
                "since the last review. No page or mail bodies."
            ),
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
    ]
    fields = [
        FieldSpec("day", FieldClass.ORDINARY),
        FieldSpec("summary", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, *, store: HouseholdStore | None = None, memory: Any = None, skills: Any = None) -> None:
        self.store = store
        self.memory = memory
        self.skills = skills
        self._state: dict[str, dict[str, Any]] = {}
        if store is not None:
            self._state = store.load_learning()

    def status(self, account_id: str) -> str:
        state = self._state.get(account_id) or {}
        reviewed = state.get("last_review") or ""
        return f"learning: last review {reviewed}" if reviewed else ""

    def due(self, now: datetime) -> list[DueWork]:
        if self.store is None:
            return []
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if moment.hour != _REVIEW_HOUR:
            return []
        work: list[DueWork] = []
        for account_id in self.store.accounts():
            if not self._needs_review(account_id, moment):
                continue

            def finish(result: str, account: str = account_id, stamp: datetime = moment) -> None:
                self._state[account] = {
                    "last_review": stamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
                }
                if self.store is not None:
                    self.store.save_learning(account, self._state[account])
                if self.skills is not None:
                    self.skills.expire_old(account, now=stamp)

            work.append(
                DueWork(
                    account_id=account_id,
                    conversation_id="learning",
                    text=REVIEW,
                    finish=finish,
                )
            )
        return work

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name != "learning_digest":
            raise NotImplementedError(tool_name)
        if self.store is None:
            return "No store."
        state = self._state.get(account_id) or {}
        since = str(state.get("last_review") or "")
        lines: list[str] = []
        for thread_id in self.store.threads(account_id):
            turns = self.store.turns(account_id, thread_id)
            for turn in turns[-40:]:
                role = turn.get("role") or ""
                text = " ".join(str(turn.get("text") or "").split())[:240]
                if not text:
                    continue
                lines.append(f"{thread_id}:{role}: {text}")
        activity = self.store.load_activity(account_id)[-80:]
        for entry in activity:
            tool = entry.get("tool") or ""
            args = entry.get("arguments") or ""
            host = ""
            if "browser_open" in tool or tool == "browser_open":
                host = host_of(str(args))
            lines.append(f"tool:{tool} {host} {args[:120]}".strip())
        if since:
            lines.insert(0, f"Since last review: {since}")
        if self.memory is not None:
            rows = getattr(self.memory, "_facts", {}).get(account_id, [])
            lines.append(f"Lessons: {len(rows)}")
        if self.skills is not None:
            rows = getattr(self.skills, "_skills", {}).get(account_id, [])
            active = sum(1 for row in rows if row.get("status") == "active")
            proposed = sum(1 for row in rows if row.get("status") == "proposed")
            lines.append(f"Skills: {active} active, {proposed} proposed")
        if not lines:
            return "Nothing new."
        return "\n".join(lines[:200])

    def _needs_review(self, account_id: str, now: datetime) -> bool:
        assert self.store is not None
        state = self._state.get(account_id) or {}
        last = str(state.get("last_review") or "")
        if last:
            try:
                stamp = datetime.fromisoformat(last)
            except ValueError:
                stamp = None
            if stamp is not None:
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if now.astimezone(timezone.utc) - stamp < timedelta(hours=20):
                    return False
        # Any recent turns in the account.
        for thread_id in self.store.threads(account_id):
            if self.store.turns(account_id, thread_id):
                return True
        return False
