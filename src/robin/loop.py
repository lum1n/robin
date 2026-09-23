"""Send one task to a model and run the tool calls it returns."""

from __future__ import annotations

from dataclasses import dataclass

from robin.airlock import redact
from robin.model import Model
from robin.policy import Route, Task
from robin.session import Assistant

SYSTEM = (
    "You are Robin, a household assistant. Use a tool when the task needs one. "
    "Answer in plain text when you already have what you need. "
    "Do not invent tool results."
)


@dataclass(frozen=True)
class Reply:
    status: str
    text: str
    route: Route
    tool: str | None = None


def converse(assistant: Assistant, task: Task, model: Model, *, max_steps: int = 4) -> Reply:
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
            return Reply("confirm", f"Confirm {call.name} before Robin does it.", decision.route, tool=call.name)
        result = outcome["result"]
        if decision.route is Route.CLOUD:
            result, _ = redact(result, vault, vocabulary=vocabulary)
        user = f"{user}\nTool {call.name} returned: {result}"
    return Reply("reply", "Stopped after the step limit.", decision.route)
