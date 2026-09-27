"""Send one task to a model and run the tool calls it returns."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

from robin.airlock import UNRESOLVED, redact, release
from robin.capability import ActiveTurn, current_task
from robin.model import Model, ToolCall
from robin.policy import Route, Task
from robin.session import Assistant

SYSTEM = (
    "You are Robin, a household assistant. "
    "Answer ordinary questions in plain text, including general knowledge. "
    "Use a tool only when the task needs that account's mail, calendar, groceries, files, photos, a web page, or an automation. "
    "If the person names a site or asks for a page, call open_page with an https URL. "
    "open_page returns a text snapshot with URL, Interactive refs, and Content. "
    "To click or type, use Interactive refs (for example target 1) or the visible name. "
    "Use select_option for dropdowns, scroll to reveal more of the page, press_key for Enter or Tab, and go_back to leave a page. "
    "Keep using tools until the person's task is done, or you need them to confirm or answer. "
    "When answering from a page, write clear prose or a short bullet list from Content. "
    "For news or a homepage, list the top stories with one line each. "
    "Skip navigation chrome, cookie banners, and repeated site chrome. "
    "Do not dump the raw snapshot. "
    "Do not use the shell to browse. "
    "Do not say a page failed to open unless open_page said so. "
    "Do not invent tool results."
)

ANSWER = (
    "You are Robin. The information below was just fetched for this person. "
    "Answer their request from that text. "
    "Write a clear, readable reply: short paragraphs or a bullet list of the main points. "
    "For news or a homepage, list the top stories with one line each. "
    "Skip navigation chrome, cookie banners, and repeated site chrome. "
    "The fetch succeeded. Do not say it failed, do not paste the raw page, and do not ask to use a tool."
)

DEFAULT_MAX_STEPS = 24


class PendingMissing(LookupError):
    pass


@dataclass(frozen=True)
class Reply:
    status: str
    text: str
    route: Route
    tool: str | None = None
    arguments: dict | None = None
    task_text: str | None = None
    allow_cloud: bool = False
    free_text: bool = False


def converse(assistant: Assistant, task: Task, model: Model, *, max_steps: int = DEFAULT_MAX_STEPS) -> Reply:
    token = current_task.set(
        ActiveTurn(task.account_id, task.conversation_id, task.allow_cloud, task.free_text)
    )
    try:
        reply = _with_site_login(assistant, task, model, max_steps=max_steps)
    finally:
        current_task.reset(token)
    assistant.remember(task.account_id, task.conversation_id, reply.status, reply.text)
    assistant.persist_vault(task.account_id, task.conversation_id)
    return reply


def _with_site_login(assistant: Assistant, task: Task, model: Model, *, max_steps: int) -> Reply:
    accepted = assistant.accept_secret(task.account_id, task.conversation_id, task.text)
    if accepted is not None and accepted.reply:
        return Reply("reply", accepted.reply, Route.LOCAL)
    if accepted is not None and accepted.resume:
        task = Task(
            account_id=task.account_id,
            conversation_id=task.conversation_id,
            text=accepted.resume,
            allow_cloud=accepted.allow_cloud,
            free_text=accepted.free_text,
        )
    else:
        peeled = assistant.peel_secret(task.account_id, task.text)
        if peeled is not None:
            task = Task(
                account_id=task.account_id,
                conversation_id=task.conversation_id,
                text=peeled,
                allow_cloud=task.allow_cloud,
                free_text=task.free_text,
            )
    return _converse(assistant, task, model, max_steps=max_steps)


def _converse(
    assistant: Assistant,
    task: Task,
    model: Model,
    *,
    max_steps: int,
    seed: tuple[str, str] | None = None,
) -> Reply:
    decision = assistant.decide(task, record=seed is None)
    vault = assistant.vaults.get(task.account_id, task.conversation_id)
    vocabulary = assistant.vocabulary.get(task.account_id, ())
    visible = decision.redacted or ""
    history = _history(assistant, task, vault, vocabulary, assistant.ner)
    spoken = _release(task.text, vault, vocabulary, assistant.ner, free_text=task.free_text)
    actions: list[str] = []
    snapshot = ""
    if seed is None:
        prepared = assistant.prepare(task.account_id, task.text)
        direct = assistant.take_direct(task.account_id)
        if direct:
            return Reply("reply", direct, Route.LOCAL)
        if prepared:
            outgoing = _release(prepared, vault, vocabulary, assistant.ner, free_text=True)
            if not outgoing or outgoing == UNRESOLVED:
                return Reply("reply", _present_fetched(prepared), Route.LOCAL)
            if not _is_page_snapshot(prepared):
                note = f"{outgoing}\nAnswer the person from the text above. Format clearly; do not paste the raw page."
                _show_egress(_compose(visible, spoken, history, note))
                turn = model.complete(
                    system=ANSWER,
                    user=_compose(visible, spoken, history, note),
                    tools=[],
                )
                return _reply_text(_grounded(turn.message, prepared), vault, vocabulary, decision.route)
            snapshot = outgoing
    else:
        tool_name, raw_result = seed
        result = _release(raw_result, vault, vocabulary, assistant.ner, free_text=True)
        actions, snapshot = _record_result(actions, snapshot, tool_name, result)

    for _ in range(max_steps):
        tools = assistant.tools(task.account_id, task.text)
        allowed = {tool["name"] for tool in tools}
        notes = _operator_notes(actions, snapshot)
        prompt = _compose(visible, spoken, history, notes, keep_end=True)
        _show_egress(prompt)
        turn = model.complete(
            system=SYSTEM,
            user=prompt,
            tools=tools,
        )
        if not turn.tool_calls:
            message = turn.message
            if snapshot:
                message = _grounded(message, snapshot)
            return _reply_text(message, vault, vocabulary, decision.route)
        call = turn.tool_calls[0]
        if call.name not in allowed:
            return Reply("reply", "That action is not available.", decision.route)
        outcome = assistant.invoke(
            task.account_id,
            task.conversation_id,
            call.name,
            call.arguments,
        )
        if outcome["status"] == "confirm":
            return Reply(
                "confirm",
                _confirm_text(assistant, task, call),
                decision.route,
                tool=call.name,
                arguments=dict(call.arguments),
                task_text=task.text,
                allow_cloud=task.allow_cloud,
                free_text=task.free_text,
            )
        result = _release(outcome["result"], vault, vocabulary, assistant.ner, free_text=True)
        if len(result) > 6000:
            result = result[:6000]
        actions, snapshot = _record_result(actions, snapshot, call.name, result)
    notes = _operator_notes(actions, snapshot)
    prompt = _compose(
        visible,
        spoken,
        history,
        notes + "\nAnswer the person now from the results above.",
        keep_end=True,
    )
    _show_egress(prompt)
    turn = model.complete(
        system=SYSTEM,
        user=prompt,
        tools=[],
    )
    message = turn.message
    if snapshot:
        message = _grounded(message, snapshot)
    return _reply_text(message, vault, vocabulary, decision.route)


_MODEL_CHARS = 6000
_SNAPSHOT_CHARS = 2200
_EXTRA_CHARS = 4500
_TURN_CHARS = 800
_HISTORY_TURNS = 16
_ACTION_LINES = 24


def _show_egress(user: str) -> None:
    if os.environ.get("ROBIN_SHOW_EGRESS") != "1":
        return
    print("--- robin egress ---", file=sys.stderr)
    print(user, file=sys.stderr)


def _release(text: str, vault, vocabulary, ner, *, free_text: bool) -> str:
    extra = ner.detect(text) if ner.available() else ()
    return release(
        text,
        vault,
        vocabulary=vocabulary,
        free_text=free_text,
        ner_available=ner.available(),
        extra=extra,
    )


def _history(assistant: Assistant, task: Task, vault, vocabulary, ner) -> str:
    """Recent turns of this conversation, after the airlock. Another conversation stays out."""
    if assistant.store is None:
        return ""
    turns = assistant.turns(task.account_id, task.conversation_id)
    if turns and turns[-1]["role"] == "user" and turns[-1]["text"] == task.text:
        turns = turns[:-1]
    lines: list[str] = []
    for turn in turns[-_HISTORY_TURNS:]:
        body = _release(" ".join(turn["text"].split()), vault, vocabulary, ner, free_text=True)
        if len(body) > _TURN_CHARS:
            body = body[:_TURN_CHARS]
        if not body:
            continue
        who = "person" if turn["role"] == "user" else "robin"
        lines.append(f"{who}: {body}")
    return "\n".join(lines)


def _compose(account: str, message: str, history: str, extra: str, *, keep_end: bool = False) -> str:
    """Fit the thread, the current words, and this turn's notes into one local context."""
    spoken = " ".join(message.split())
    extra = extra.strip()
    if len(extra) > _EXTRA_CHARS:
        extra = extra[-_EXTRA_CHARS:] if keep_end else extra[:_EXTRA_CHARS]
    lines = [line for line in history.split("\n") if line]
    snapshot = account or ""
    if len(snapshot) > _SNAPSHOT_CHARS:
        snapshot = _keep_message(snapshot, spoken, _SNAPSHOT_CHARS)

    def pack(kept: list[str], shot: str, note: str) -> str:
        parts: list[str] = []
        if kept:
            parts.append("Conversation:\n" + "\n".join(kept))
        if shot:
            parts.append(shot)
        if note:
            parts.append(note)
        return "\n".join(parts)

    text = pack(lines, snapshot, extra)
    if len(text) <= _MODEL_CHARS:
        return text
    snapshot = _keep_message(snapshot, spoken, min(len(snapshot), 500))
    text = pack(lines, snapshot, extra)
    while lines and len(text) > _MODEL_CHARS:
        lines = lines[1:]
        text = pack(lines, snapshot, extra)
    if len(text) <= _MODEL_CHARS:
        return text
    room = _MODEL_CHARS - len(pack(lines, snapshot, "")) - 1
    if room > 80 and extra:
        extra = extra[-room:] if keep_end else extra[:room]
        return pack(lines, snapshot, extra)
    note = extra[-1000:] if keep_end else extra[:1000]
    return pack(lines[-4:], _keep_message(spoken, spoken, 400), note)


_DENIAL = re.compile(
    r"(unable to|cannot open|can't open|could not open|couldn't open|failed to open|"
    r"not installed|cannot read|can't read|could not read|don't have access|do not have access|"
    r"cannot log|can't log|could not log|unable to log)",
    re.IGNORECASE,
)


def _present_fetched(prepared: str) -> str:
    """Show fetched free text on this machine when it cannot leave for a model."""
    text = prepared.strip()
    if text.startswith("URL:"):
        content = text
        if "\nContent:\n" in text:
            content = text.split("\nContent:\n", 1)[1]
        body = _page_lines(content)
        header = "\n".join(text.splitlines()[:2])
        if body:
            return f"{header}\n{body}"
        return header
    if text.startswith("Opened "):
        first, _, rest = text.partition("\n")
        body = _page_lines(rest)
        if body:
            return f"{first}\n{body}"
        return first
    body = _page_lines(text)
    return body or text


def _page_lines(text: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        if len(line) < 3:
            continue
        key = line.casefold()
        if key in seen:
            continue
        if _PAGE_NOISE.search(line):
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= 24:
            break
    return "\n".join(lines)


_PAGE_NOISE = re.compile(
    r"^(cookie|cookies|accept all|reject all|godta alle|meny|menu|search|logg?\s*inn|"
    r"sign in|subscribe|abonner|advertisement|annonse)\b",
    re.IGNORECASE,
)


def _grounded(message: str, prepared: str) -> str:
    """A fetched page or inbox stands when the model denies that the fetch happened."""
    fetched = prepared.strip()
    text = message.strip()
    if not fetched:
        return text
    if not text or _DENIAL.search(text):
        return _present_fetched(fetched)
    return text


def _keep_message(snapshot: str, spoken: str, room: int) -> str:
    if room <= 0:
        return spoken[:_TURN_CHARS]
    if len(snapshot) <= room:
        return snapshot
    if spoken and spoken not in snapshot[:room]:
        keep = room - len(spoken) - 1
        if keep > 0:
            return f"{snapshot[:keep]}\n{spoken}"
        return spoken[:room]
    return snapshot[:room]


def _is_page_snapshot(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("URL:") or "\nURL:" in text


def _split_snapshot(result: str) -> tuple[str, str]:
    """Separate a short action line from a page snapshot when both are present."""
    if "\nURL:" in result and not result.lstrip().startswith("URL:"):
        lead, _, rest = result.partition("\nURL:")
        return lead.strip(), "URL:" + rest
    if result.lstrip().startswith("URL:"):
        return "", result.lstrip()
    return result.strip(), ""


def _record_result(
    actions: list[str],
    snapshot: str,
    tool_name: str,
    result: str,
) -> tuple[list[str], str]:
    lead, page = _split_snapshot(result)
    if page:
        if lead:
            actions = [*actions, lead][-_ACTION_LINES:]
        else:
            actions = [*actions, f"{tool_name}"][-_ACTION_LINES:]
        return actions, page
    line = lead or result
    if len(line) > 500:
        line = line[:500]
    return [*actions, f"Tool {tool_name} returned: {line}"][-_ACTION_LINES:], snapshot


def _operator_notes(actions: list[str], snapshot: str) -> str:
    parts: list[str] = []
    if actions:
        parts.append("Action log:\n" + "\n".join(actions))
    if snapshot:
        parts.append("Current page:\n" + snapshot)
    return "\n\n".join(parts)


def _reply_text(message: str, vault, vocabulary, route: Route) -> Reply:
    text = message.strip() or "I could not finish that."
    shown, _ = redact(text, vault, vocabulary=vocabulary)
    return Reply("reply", vault.restore(shown), route)


def _confirm_text(assistant: Assistant, task: Task, call: ToolCall) -> str:
    base = f"Confirm {call.name} before Robin does it."
    try:
        _capability, tool = assistant.registry.resolve(task.account_id, call.name)
    except KeyError:
        return base
    logged = {key: "" if key in tool.drop_arguments else value for key, value in call.arguments.items()}
    if not any(str(value) for value in logged.values()):
        return base
    vault = assistant.vaults.get(task.account_id, task.conversation_id)
    shown, _report = redact(json.dumps(logged, sort_keys=True), vault, vocabulary=assistant.vocabulary.get(task.account_id, ()))
    return f"{base} {vault.restore(shown)}"


def resume(
    assistant: Assistant,
    account_id: str,
    conversation_id: str,
    model: Model | None = None,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Reply:
    pending = assistant.take_pending(account_id, conversation_id)
    if pending is None:
        raise PendingMissing(conversation_id)
    outcome = assistant.invoke(
        account_id,
        conversation_id,
        pending["tool"],
        pending["arguments"],
        confirmed=True,
    )
    route = Route(pending["route"])
    if model is None:
        reply = Reply("reply", outcome["result"], route, tool=pending["tool"])
        assistant.remember(account_id, conversation_id, reply.status, reply.text)
        assistant.persist_vault(account_id, conversation_id)
        return reply
    task = Task(
        account_id=account_id,
        conversation_id=conversation_id,
        text=str(pending.get("text") or "continue"),
        allow_cloud=bool(pending.get("allow_cloud")),
        free_text=bool(pending.get("free_text")),
    )
    token = current_task.set(
        ActiveTurn(task.account_id, task.conversation_id, task.allow_cloud, task.free_text)
    )
    try:
        reply = _converse(
            assistant,
            task,
            model,
            max_steps=max_steps,
            seed=(str(pending["tool"]), outcome["result"]),
        )
    finally:
        current_task.reset(token)
    if reply.route is not route and reply.status == "reply":
        reply = Reply(
            reply.status,
            reply.text,
            route,
            tool=reply.tool,
            arguments=reply.arguments,
            task_text=reply.task_text,
            allow_cloud=reply.allow_cloud,
            free_text=reply.free_text,
        )
    assistant.remember(account_id, conversation_id, reply.status, reply.text)
    assistant.persist_vault(account_id, conversation_id)
    return reply
