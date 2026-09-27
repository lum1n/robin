"""A model turn. The client speaks to an OpenAI-compatible server. The airlock decides what it may see."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import URLError
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

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        transport: Transport | None = None,
        model: str = "local",
        api_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport or (lambda url, body: urllib_transport(url, body, api_key=api_key))
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
        try:
            payload = self.transport(f"{self.base_url}/v1/chat/completions", body)
        except TimeoutError:
            return ModelTurn("The model did not answer in time.")
        except URLError as exc:
            reason = str(exc.reason).lower()
            if isinstance(exc.reason, TimeoutError) or "timed out" in reason:
                return ModelTurn("The model did not answer in time.")
            return ModelTurn("The model is not running.")
        return _parse(payload)


def urllib_transport(url: str, body: dict[str, Any], api_key: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=240) as response:  # noqa: S310
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
