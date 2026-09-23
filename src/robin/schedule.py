"""A check this account asked for. It stays off until they turn it on."""

from __future__ import annotations

from robin.loop import Reply, converse
from robin.model import Model
from robin.policy import Task
from robin.session import Assistant

CONVERSATION = "schedule"
CHECK = "Check this account's mail and calendar. Say what needs attention. Do not send, pay, or delete."


def tick(assistant: Assistant, model: Model) -> list[Reply]:
    replies: list[Reply] = []
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
