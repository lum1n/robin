"""A model turn. The default client speaks to a local OpenAI-compatible server."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    message: str
    tool_calls: tuple[ToolCall, ...] = ()


class Model(Protocol):
    def complete(self, *, system: str, user: str, tools: list[dict[str, Any]]) -> ModelTurn: ...


Transport = Any


class ChatModel:
    """Posts one chat completion. Pass a transport in tests so nothing is sent."""

    def __init__(self, base_url: str = "http://127.0.0.1:8080", *, transport: Transport | None = None, model: str = "local") -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport or urllib_transport
        self.model = model

    def complete(self, *, system: str, user: str, tools: list[dict[str, Any]]) -> ModelTurn:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [_function(tool) for tool in tools],
        }
        payload = self.transport(f"{self.base_url}/v1/chat/completions", body)
        return _parse(payload)


def urllib_transport(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read().decode())


def _function(tool: dict[str, Any]) -> dict[str, Any]:
    parameters = tool.get("parameters") or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": parameters,
        },
    }


def _parse(payload: dict[str, Any]) -> ModelTurn:
    message = payload["choices"][0]["message"]
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        if isinstance(raw, str):
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {}
        else:
            arguments = dict(raw)
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(ToolCall(name=str(function.get("name") or ""), arguments=arguments))
    text = message.get("content") or ""
    return ModelTurn(message=str(text), tool_calls=tuple(calls))
