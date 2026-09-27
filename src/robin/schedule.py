"""A check this account asked for, and automations they described."""

from __future__ import annotations

from datetime import datetime

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
            )
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
            )
        replies.append(reply)
    return replies
