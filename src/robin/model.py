"""A model turn. The client speaks to an OpenAI-compatible or Anthropic server. The airlock decides what it may see."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from robin.vault import REFERENCE

NEARAI_BASE = "https://cloud-api.near.ai"
# Flash TEE default: full Qwen+tools was regularly 30s–minutes for "hi".
NEARAI_DEFAULT_MODEL = "z-ai/glm-5.3-flash"
NEARAI_PRIVACY_FILTER = "openai/privacy-filter"
NEARAI_CHAT_TIMEOUT = 60.0
NEARAI_CLASSIFY_TIMEOUT = 20.0
_PROTECTED_MARKERS = re.compile(r"\[(?:REDACTED|UNRESOLVED)\]")
_SCRUB_CACHE_MAX = 256
_CLASSIFY_BATCH = 16


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = "call_0"
    error: str = ""


@dataclass(frozen=True)
class ModelTurn:
    message: str
    tool_calls: tuple[ToolCall, ...] = ()


class Model(Protocol):
    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn: ...


Transport = Any
CatalogFetch = Callable[[], dict[str, Any]]


class TeeModelError(ValueError):
    """Configured model is not a NEAR AI TEE / verifiable chat model."""


class ChatModel:
    """Posts one OpenAI-style chat completion. Pass a transport in tests so nothing is sent."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        transport: Transport | None = None,
        model: str = "local",
        api_key: str = "",
        timeout: float = 240.0,
        retry: bool = True,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retry = retry
        self.extra_body = dict(extra_body or {})
        self.transport = transport or (
            lambda url, body: urllib_transport(url, body, api_key=api_key, timeout=timeout)
        )
        self.model = model

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            body["tools"] = [_function(tool) for tool in tools]
        if self.extra_body:
            body.update(self.extra_body)
        try:
            payload = _send(
                self.transport,
                f"{self.base_url}/v1/chat/completions",
                body,
                retry=self.retry,
            )
        except HTTPError as exc:
            return ModelTurn(_http_problem(exc))
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


class NearAiModel:
    """NEAR AI Cloud chat: TEE-only models, privacy classify after Robin's airlock, then completion."""

    def __init__(
        self,
        base_url: str = NEARAI_BASE,
        *,
        transport: Transport | None = None,
        model: str = NEARAI_DEFAULT_MODEL,
        api_key: str = "",
        catalog: dict[str, Any] | None = None,
        catalog_fetch: CatalogFetch | None = None,
        privacy: bool | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.transport = transport or (
            lambda url, body: urllib_transport(url, body, api_key=api_key, timeout=NEARAI_CHAT_TIMEOUT)
        )
        self._classify_transport = transport or (
            lambda url, body: urllib_transport(url, body, api_key=api_key, timeout=NEARAI_CLASSIFY_TIMEOUT)
        )
        self._catalog_fetch = catalog_fetch or (lambda: urllib_get_json(f"{self.base_url}/v1/model/list"))
        # TEE chat models (GLM/Qwen/…) reason by default; that multiplies latency on every
        # browser/tool step (Finn.no jobs can take dozens of completes).
        self._chat = ChatModel(
            self.base_url,
            transport=self.transport,
            model=model,
            api_key=api_key,
            timeout=NEARAI_CHAT_TIMEOUT,
            retry=False,
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": False,
                    "thinking": False,
                },
                "thinking": {"type": "disabled"},
            },
        )
        self._scrub_cache: OrderedDict[str, str] = OrderedDict()
        if privacy is None:
            # Off by default: the TEE privacy pass was dominating latency. Opt in with
            # ROBIN_NEARAI_PRIVACY=1 when you want the second filter again.
            flag = os.environ.get("ROBIN_NEARAI_PRIVACY", "0").strip().lower()
            privacy = flag in {"1", "true", "yes", "on"}
        self.privacy = privacy
        if catalog is not None:
            assert_tee_chat_model(model, catalog)
            self._tee_checked = True
        else:
            self._tee_checked = False

    def ensure_tee(self) -> None:
        if self._tee_checked:
            return
        assert_tee_chat_model(self.model, self._catalog_fetch())
        self._tee_checked = True

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        try:
            self.ensure_tee()
        except TeeModelError as exc:
            return ModelTurn(str(exc))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeError, ValueError, KeyError, TypeError):
            return ModelTurn("Robin could not verify that the model runs in a TEE.")
        scrubbed = messages
        if self.privacy:
            try:
                scrubbed = self._privacy_scrub(messages)
            except HTTPError as exc:
                return ModelTurn(_privacy_problem(exc))
            except TimeoutError:
                return ModelTurn("The privacy filter did not answer in time.")
            except URLError as exc:
                reason = str(exc.reason).lower()
                if isinstance(exc.reason, TimeoutError) or "timed out" in reason:
                    return ModelTurn("The privacy filter did not answer in time.")
                return ModelTurn("The privacy filter is not reachable.")
            except (OSError, json.JSONDecodeError, UnicodeError, ValueError, KeyError, IndexError, TypeError):
                return ModelTurn("The privacy filter refused the request.")
        print(
            f"robin: near.ai chat {self.model} messages={len(scrubbed)} tools={len(tools)}",
            file=sys.stderr,
            flush=True,
        )
        return self._chat.complete(messages=scrubbed, tools=tools)

    def _privacy_scrub(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Classify new non-system airlock strings; cache and batch to cut cost/latency.

        System messages are Robin-authored plus already-released local lines. They also
        change every turn (clock/context), so classifying them defeated the cache and
        dominated latency. User / assistant / tool strings still get the TEE second pass.
        """
        slots: list[tuple[int, str]] = []
        for index, message in enumerate(messages):
            content = message.get("content")
            if not isinstance(content, str) or not content:
                continue
            # Trusted local scaffold — airlock already ran on any personal fragments.
            if message.get("role") == "system":
                continue
            slots.append((index, content))
        if not slots:
            return messages

        pending: list[str] = []
        seen: set[str] = set()
        for _, text in slots:
            if text in self._scrub_cache:
                self._scrub_cache.move_to_end(text)
                continue
            if text in seen:
                continue
            seen.add(text)
            if not needs_privacy_classify(text):
                self._remember_scrub(text, text)
                continue
            pending.append(text)

        if pending:
            print(
                f"robin: near.ai privacy classify {len(pending)} new string(s)",
                file=sys.stderr,
                flush=True,
            )
        for offset in range(0, len(pending), _CLASSIFY_BATCH):
            chunk = pending[offset : offset + _CLASSIFY_BATCH]
            self._classify_and_cache(chunk)

        out = [dict(message) for message in messages]
        for message_index, text in slots:
            out[message_index]["content"] = self._scrub_cache[text]
        return out

    def _classify_and_cache(self, texts: list[str]) -> None:
        if not texts:
            return
        url = f"{self.base_url}/v1/privacy/classify"
        try:
            payload = _send(
                self._classify_transport,
                url,
                {"model": NEARAI_PRIVACY_FILTER, "input": texts if len(texts) > 1 else texts[0]},
                retry=True,
            )
            for index, text in enumerate(texts):
                spans = _classify_spans(payload, expected_index=index, allow_single=(len(texts) == 1))
                self._remember_scrub(text, apply_privacy_spans(text, spans))
            return
        except (HTTPError, ValueError, KeyError, IndexError, TypeError):
            if len(texts) == 1:
                raise
        # Batch failed: fall back per string so one oversized peer does not block the rest.
        for text in texts:
            if text in self._scrub_cache:
                continue
            payload = _send(
                self._classify_transport,
                url,
                {"model": NEARAI_PRIVACY_FILTER, "input": text},
                retry=True,
            )
            spans = _classify_spans(payload, expected_index=0, allow_single=True)
            self._remember_scrub(text, apply_privacy_spans(text, spans))

    def _remember_scrub(self, original: str, scrubbed: str) -> None:
        self._scrub_cache[original] = scrubbed
        self._scrub_cache.move_to_end(original)
        while len(self._scrub_cache) > _SCRUB_CACHE_MAX:
            self._scrub_cache.popitem(last=False)


_ROBIN_FRAMING = re.compile(
    r"(?m)^(?:Person|Conversation|Connectors|Learned lessons|Learned skills):\s*"
)


def needs_privacy_classify(text: str) -> bool:
    """False when only Robin placeholders/markers/framing remain — skip a billed TEE classify call."""
    remainder = REFERENCE.sub("", text)
    remainder = _PROTECTED_MARKERS.sub("", remainder)
    remainder = _ROBIN_FRAMING.sub("", remainder)
    return any(char.isalnum() for char in remainder)


def _classify_spans(
    payload: dict[str, Any],
    *,
    expected_index: int = 0,
    allow_single: bool = False,
) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("privacy classify returned no results")
    entry = None
    for item in data:
        if isinstance(item, dict) and int(item.get("index", -1)) == expected_index:
            entry = item
            break
    if entry is None and allow_single and len(data) == 1 and isinstance(data[0], dict):
        entry = data[0]
    if entry is None:
        raise ValueError("privacy classify missed an input")
    spans = entry.get("spans")
    if spans is None:
        return []
    if not isinstance(spans, list):
        raise ValueError("privacy classify spans were unreadable")
    return [span for span in spans if isinstance(span, dict)]


def apply_privacy_spans(text: str, spans: list[dict[str, Any]]) -> str:
    """Replace classified PII spans. Leave Robin placeholders and markers alone."""
    protected = [(match.start(), match.end()) for match in REFERENCE.finditer(text)]
    protected.extend((match.start(), match.end()) for match in _PROTECTED_MARKERS.finditer(text))
    candidates: list[tuple[int, int, str]] = []
    for span in spans:
        try:
            start = int(span["start"])
            end = int(span["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < 0 or end > len(text) or start >= end:
            continue
        if any(start < pe and end > ps for ps, pe in protected):
            continue
        category = str(span.get("category") or "").strip().lower()
        candidates.append((start, end, category))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str]] = []
    for start, end, category in candidates:
        if any(start < other_end and end > other_start for other_start, other_end, _ in selected):
            continue
        selected.append((start, end, category))
    selected.sort(key=lambda item: item[0], reverse=True)
    out = text
    for start, end, category in selected:
        out = out[:start] + _privacy_replacement(category) + out[end:]
    return out


def _privacy_replacement(_category: str) -> str:
    # Residual second-pass hits are never restored into the vault.
    return "[REDACTED]"


def _privacy_problem(exc: HTTPError) -> str:
    detail = _http_problem(exc)
    if "model" in detail.lower():
        return detail.replace("model", "privacy filter", 1).replace("Model", "Privacy filter", 1)
    return detail


def assert_tee_chat_model(model_id: str, catalog: dict[str, Any]) -> None:
    """Raise TeeModelError unless model_id is a verifiable, attestable chat model (not the privacy filter)."""
    if model_id == NEARAI_PRIVACY_FILTER:
        raise TeeModelError("The privacy filter is not a chat model.")
    models = catalog.get("models")
    if models is None and isinstance(catalog.get("data"), list):
        models = catalog["data"]
    if not isinstance(models, list):
        raise TeeModelError("Robin could not read the NEAR AI model catalog.")
    entry: dict[str, Any] | None = None
    for item in models:
        if not isinstance(item, dict):
            continue
        mid = item.get("modelId") or item.get("id") or item.get("model")
        if mid == model_id:
            entry = item
            break
    if entry is None:
        raise TeeModelError(f"Model {model_id!r} is not in the NEAR AI catalog.")
    meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else entry
    verifiable = bool(meta.get("verifiable"))
    attestation = bool(meta.get("attestationSupported"))
    if not verifiable or not attestation:
        raise TeeModelError(f"Model {model_id!r} is not a TEE / verifiable NEAR AI model.")
    modalities = meta.get("output_modalities") or (meta.get("architecture") or {}).get("outputModalities") or []
    if isinstance(modalities, list) and modalities and "classification" in modalities and "text" not in modalities:
        raise TeeModelError(f"Model {model_id!r} is not a chat model.")


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
            payload = _send(self.transport, f"{self.base_url}/v1/messages", body)
        except HTTPError as exc:
            return ModelTurn(_http_problem(exc))
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
    timeout: float = 240.0,
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
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode())


def urllib_get_json(url: str, api_key: str = "") -> dict[str, Any]:
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read().decode())


def _send(transport: Transport, url: str, body: dict[str, Any], *, retry: bool = True) -> dict[str, Any]:
    """Optional one retry when the provider is rate limited or briefly failing."""
    try:
        return transport(url, body)
    except HTTPError as exc:
        if not retry or (exc.code != 429 and exc.code < 500):
            raise
        time.sleep(_retry_after(exc))
        return transport(url, body)


def _retry_after(exc: HTTPError) -> float:
    try:
        return min(max(float(exc.headers.get("Retry-After") or 2), 0.5), 10.0)
    except (AttributeError, TypeError, ValueError):
        return 2.0


def _http_problem(exc: HTTPError) -> str:
    try:
        detail = exc.read().decode(errors="replace")[:2000]
    except Exception:  # noqa: BLE001
        detail = ""
    print(f"robin: model HTTP {exc.code}: {detail}", file=sys.stderr, flush=True)
    lowered = detail.lower()
    if exc.code == 429:
        return "The model is rate limited right now. Try again in a moment."
    if exc.code in (401, 403):
        return "The model rejected Robin's API key."
    if "context" in lowered and ("length" in lowered or "window" in lowered) or "too long" in lowered or exc.code == 413:
        return "That conversation got too long for the model. Start a new thread and ask again."
    if exc.code >= 500:
        return "The model service had an error. Try again in a moment."
    return f"The model refused the request (HTTP {exc.code})."


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


def decode_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    """Tool-call arguments as a dict, plus an error when the model sent something unusable."""
    if raw is None or raw == "":
        return {}, ""
    value = raw
    # Local models sometimes double-encode arguments or wrap them in prose or code fences.
    for _ in range(3):
        if not isinstance(value, str):
            break
        text = value.strip()
        if not text:
            return {}, ""
        try:
            value = json.loads(text)
            continue
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            break
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            break
    if isinstance(value, dict):
        return dict(value), ""
    shown = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    return {}, f"arguments were not a JSON object: {shown[:200]}"


_THINK_BLOCK = re.compile(
    r"<(?:think|thinking|reason|reasoning)>[\s\S]*?</(?:think|thinking|reason|reasoning)>\s*",
    re.IGNORECASE,
)
_THINK_CLOSE = re.compile(r"</(?:think|thinking|reason|reasoning)>\s*", re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Drop model chain-of-thought that leaked into content (GLM/Qwen think tags)."""
    if not text:
        return ""
    cleaned = _THINK_BLOCK.sub("", text)
    if _THINK_CLOSE.search(cleaned):
        cleaned = _THINK_CLOSE.split(cleaned)[-1]
    return cleaned.strip()


def _parse_openai(payload: dict[str, Any]) -> ModelTurn:
    message = payload["choices"][0]["message"]
    calls: list[ToolCall] = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        arguments, error = decode_arguments(function.get("arguments"))
        calls.append(
            ToolCall(
                id=str(call.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
                error=error,
            )
        )
    # Never surface reasoning_content — only the final answer text.
    text = strip_thinking(str(message.get("content") or ""))
    return ModelTurn(message=text, tool_calls=tuple(calls))


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
            arguments, error = decode_arguments(block.get("input"))
            calls.append(
                ToolCall(
                    id=str(block.get("id") or f"call_{len(calls)}"),
                    name=str(block.get("name") or ""),
                    arguments=arguments,
                    error=error,
                )
            )
    return ModelTurn(message="".join(texts), tool_calls=tuple(calls))
