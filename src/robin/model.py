"""A model turn. The client speaks to an OpenAI-compatible or Anthropic server. The airlock decides what it may see."""

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
    id: str = "call_0"


@dataclass(frozen=True)
class ModelTurn:
    message: str
    tool_calls: tuple[ToolCall, ...] = ()


class Model(Protocol):
    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn: ...


Transport = Any


class ChatModel:
    """Posts one OpenAI-style chat completion. Pass a transport in tests so nothing is sent."""

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

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            body["tools"] = [_function(tool) for tool in tools]
        try:
            payload = self.transport(f"{self.base_url}/v1/chat/completions", body)
        except TimeoutError:
            return ModelTurn("The model did not answer in time.")
        except URLError as exc:
            reason = str(exc.reason).lower()
            if isinstance(exc.reason, TimeoutError) or "timed out" in reason:
                return ModelTurn("The model did not answer in time.")
            return ModelTurn("The model is not running.")
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            return ModelTurn("The model sent a reply Robin could not read.")
        try:
            return _parse_openai(payload)
        except (KeyError, IndexError, TypeError, ValueError):
            return ModelTurn("The model sent a reply Robin could not read.")


class AnthropicModel:
    """Posts one Anthropic Messages request. Same Model protocol as ChatModel."""

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com",
        *,
        transport: Transport | None = None,
        model: str = "claude-sonnet-4-20250514",
        api_key: str = "",
        version: str = "2023-06-01",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.version = version
        self.transport = transport or (
            lambda url, body: urllib_transport(
                url,
                body,
                api_key=api_key,
                headers={
                    "anthropic-version": version,
                    "x-api-key": api_key,
                },
            )
        )

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        system = ""
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                system = str(message.get("content") or "")
                continue
            if role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.get("tool_call_id") or "",
                                "content": str(message.get("content") or ""),
                            }
                        ],
                    }
                )
                continue
            if role == "assistant" and message.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                text = message.get("content") or ""
                if text:
                    blocks.append({"type": "text", "text": text})
                for call in message["tool_calls"]:
                    function = call.get("function") or {}
                    raw = function.get("arguments") or "{}"
                    try:
                        arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                    except json.JSONDecodeError:
                        arguments = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id") or "",
                            "name": function.get("name") or "",
                            "input": arguments,
                        }
                    )
                converted.append({"role": "assistant", "content": blocks})
                continue
            converted.append({"role": role, "content": str(message.get("content") or "")})
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": converted,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]
        try:
            payload = self.transport(f"{self.base_url}/v1/messages", body)
        except TimeoutError:
            return ModelTurn("The model did not answer in time.")
        except URLError as exc:
            reason = str(exc.reason).lower()
            if isinstance(exc.reason, TimeoutError) or "timed out" in reason:
                return ModelTurn("The model did not answer in time.")
            return ModelTurn("The model is not running.")
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            return ModelTurn("The model sent a reply Robin could not read.")
        try:
            return _parse_anthropic(payload)
        except (KeyError, IndexError, TypeError, ValueError):
            return ModelTurn("The model sent a reply Robin could not read.")


def urllib_transport(
    url: str,
    body: dict[str, Any],
    api_key: str = "",
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    elif api_key:
        request_headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers=request_headers,
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


def _parse_openai(payload: dict[str, Any]) -> ModelTurn:
    message = payload["choices"][0]["message"]
    calls: list[ToolCall] = []
    for index, call in enumerate(message.get("tool_calls") or []):
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
        calls.append(
            ToolCall(
                id=str(call.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    text = message.get("content") or ""
    return ModelTurn(message=str(text), tool_calls=tuple(calls))


def _parse_anthropic(payload: dict[str, Any]) -> ModelTurn:
    blocks = payload.get("content") or []
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            arguments = block.get("input") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(
                ToolCall(
                    id=str(block.get("id") or f"call_{len(calls)}"),
                    name=str(block.get("name") or ""),
                    arguments=arguments,
                )
            )
    return ModelTurn(message="".join(texts), tool_calls=tuple(calls))
