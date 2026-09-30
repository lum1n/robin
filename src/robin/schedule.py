"""A check this account asked for, and automations they described."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from robin.loop import Reply, converse
from robin.model import Model
from robin.policy import Task
from robin.session import Assistant

CONVERSATION = "schedule"
CHECK = "Check this account's mail and calendar. Say what needs attention. Do not send, pay, or delete."


def run_due(assistant: Assistant, model: Model, *, now: datetime | None = None) -> list[Reply]:
    moment = now or datetime.now()
    replies: list[Reply] = []
    for work in assistant.due(moment):
        try:
            reply = converse(
                assistant,
                Task(work.account_id, work.conversation_id, work.text, allow_cloud=False),
                model,
            )
        except Exception:
            continue
        if reply.status == "confirm" and reply.tool:
            assistant.set_pending(
                work.account_id,
                work.conversation_id,
                reply.tool,
                reply.arguments or {},
                reply.route.value,
                text=reply.task_text or work.text,
                allow_cloud=reply.allow_cloud,
                free_text=reply.free_text,
            )
        _notify_attention(assistant, work.account_id, work.conversation_id, reply)
        work.finish(reply.text)
        replies.append(reply)
    return replies


def tick(assistant: Assistant, model: Model, *, now: datetime | None = None) -> list[Reply]:
    replies = run_due(assistant, model, now=now)
    for account_id in assistant.scheduled_accounts():
        reply = converse(
            assistant,
            Task(account_id, CONVERSATION, CHECK, allow_cloud=False),
            model,
        )
        if reply.status == "confirm" and reply.tool:
            assistant.set_pending(
                account_id,
                CONVERSATION,
                reply.tool,
                reply.arguments or {},
                reply.route.value,
                text=reply.task_text or CHECK,
                allow_cloud=reply.allow_cloud,
                free_text=reply.free_text,
            )
        _notify_attention(assistant, account_id, CONVERSATION, reply)
        replies.append(reply)
    return replies


def _notify_attention(assistant: Assistant, account_id: str, conversation_id: str, reply: Reply) -> None:
    """Enqueue attention when background work needs the person (confirm or input)."""
    if reply.status not in {"confirm", "input"}:
        return
    notify = _find_notify(assistant)
    if notify is None:
        return
    enqueue = getattr(notify, "enqueue", None)
    if not callable(enqueue):
        return
    if reply.status == "confirm":
        tool = reply.tool or "an action"
        text = f"Robin needs confirmation for {tool}."
        kind = "confirm"
    else:
        request = reply.input
        title = getattr(request, "title", None) if request is not None else None
        text = str(reply.text or title or "Robin needs a detail")[:500]
        kind = "input"
    enqueue(account_id, text, kind=kind, conversation_id=conversation_id)


def _find_notify(assistant: Assistant) -> Any | None:
    for capability in getattr(assistant.registry, "_capabilities", []):
        if getattr(capability, "id", "") == "notify":
            return capability
    return None
