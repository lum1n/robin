"""Send one task to a model and run the tool calls it returns."""

from __future__ import annotations

import json
from dataclasses import dataclass

from robin.airlock import redact
from robin.model import Model, ToolCall
from robin.policy import Route, Task
from robin.session import Assistant

SYSTEM = (
    "You are Robin, a household assistant. Use a tool when the task needs one. "
    "Answer in plain text when you already have what you need. "
    "Do not invent tool results."
)


class PendingMissing(LookupError):
    pass


@dataclass(frozen=True)
class Reply:
    status: str
    text: str
    route: Route
    tool: str | None = None
    arguments: dict | None = None


def converse(assistant: Assistant, task: Task, model: Model, *, max_steps: int = 4) -> Reply:
    reply = _converse(assistant, task, model, max_steps=max_steps)
    assistant.remember(task.account_id, task.conversation_id, reply.status, reply.text)
    assistant.persist_vault(task.account_id, task.conversation_id)
    return reply


def _converse(assistant: Assistant, task: Task, model: Model, *, max_steps: int) -> Reply:
    decision = assistant.decide(task)
    vault = assistant.vaults.get(task.account_id, task.conversation_id)
    vocabulary = assistant.vocabulary.get(task.account_id, ())
    visible = decision.cloud_payload if decision.route is Route.CLOUD else decision.local_text
    tools = assistant.tools(task.account_id)
    allowed = {tool["name"] for tool in tools}
    user = visible or ""
    for _ in range(max_steps):
        turn = model.complete(system=SYSTEM, user=user, tools=tools)
        if not turn.tool_calls:
            shown, _ = redact(turn.message, vault, vocabulary=vocabulary)
            return Reply("reply", vault.restore(shown), decision.route)
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
            )
        result = outcome["result"]
        if decision.route is Route.CLOUD:
            result, _ = redact(result, vault, vocabulary=vocabulary)
        user = f"{user}\nTool {call.name} returned: {result}"
    return Reply("reply", "Stopped after the step limit.", decision.route)


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


def resume(assistant: Assistant, account_id: str, conversation_id: str) -> Reply:
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
    reply = Reply("reply", outcome["result"], route, tool=pending["tool"])
    assistant.remember(account_id, conversation_id, reply.status, reply.text)
    assistant.persist_vault(account_id, conversation_id)
    return reply
