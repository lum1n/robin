"""NEAR AI TEE provider: catalog gate, privacy classify + local apply, fail closed."""

from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError

import pytest

from robin.model import (
    NEARAI_DEFAULT_MODEL,
    NEARAI_PRIVACY_FILTER,
    ChatModel,
    NearAiModel,
    TeeModelError,
    apply_privacy_spans,
    assert_tee_chat_model,
    strip_thinking,
)


def _tee_catalog(model_id: str = NEARAI_DEFAULT_MODEL, *, verifiable: bool = True, attestation: bool = True) -> dict:
    return {
        "models": [
            {
                "modelId": model_id,
                "metadata": {
                    "verifiable": verifiable,
                    "attestationSupported": attestation,
                    "architecture": {"outputModalities": ["text"]},
                },
            },
            {
                "modelId": NEARAI_PRIVACY_FILTER,
                "metadata": {
                    "verifiable": True,
                    "attestationSupported": True,
                    "architecture": {"outputModalities": ["classification"]},
                },
            },
            {
                "modelId": "openai/gpt-5",
                "metadata": {
                    "verifiable": False,
                    "attestationSupported": False,
                    "architecture": {"outputModalities": ["text"]},
                },
            },
        ]
    }


def test_assert_tee_refuses_incognito_and_privacy_filter() -> None:
    catalog = _tee_catalog()
    assert_tee_chat_model(NEARAI_DEFAULT_MODEL, catalog)
    with pytest.raises(TeeModelError, match="not a TEE"):
        assert_tee_chat_model("openai/gpt-5", catalog)
    with pytest.raises(TeeModelError, match="privacy filter"):
        assert_tee_chat_model(NEARAI_PRIVACY_FILTER, catalog)
    with pytest.raises(TeeModelError, match="not in the NEAR AI catalog"):
        assert_tee_chat_model("missing/model", catalog)


def test_nearai_refuses_non_tee_model_at_construct() -> None:
    with pytest.raises(TeeModelError, match="not a TEE"):
        NearAiModel(model="openai/gpt-5", api_key="sk-test", catalog=_tee_catalog("openai/gpt-5", verifiable=False))


def test_apply_privacy_spans_skips_robin_placeholders() -> None:
    placeholder = "[PERSON_deadbeefdeadbeefdeadbeefdeadbeef_1]"
    text = f"{placeholder} called jane leftover@example.com"
    scrubbed = apply_privacy_spans(
        text,
        [
            {"category": "private_person", "start": 0, "end": len(placeholder), "text": placeholder},
            {"category": "private_person", "start": text.index("jane"), "end": text.index("jane") + 4},
            {
                "category": "private_email",
                "start": text.index("leftover@example.com"),
                "end": text.index("leftover@example.com") + len("leftover@example.com"),
            },
        ],
    )
    assert placeholder in scrubbed
    assert "jane" not in scrubbed
    assert "leftover@example.com" not in scrubbed
    assert scrubbed.count("[REDACTED]") == 2


def test_privacy_classify_runs_before_chat_and_placeholders_survive() -> None:
    calls: list[tuple[str, dict]] = []
    placeholder = "[PERSON_deadbeefdeadbeefdeadbeefdeadbeef_1]"
    original = f"{placeholder} called jane leftover@example.com"

    def transport(url: str, body: dict) -> dict:
        calls.append((url, body))
        if url.endswith("/v1/privacy/classify"):
            assert body["model"] == NEARAI_PRIVACY_FILTER
            assert body["input"] == original
            return {
                "model": NEARAI_PRIVACY_FILTER,
                "data": [
                    {
                        "index": 0,
                        "spans": [
                            {
                                "category": "private_email",
                                "start": original.index("leftover@example.com"),
                                "end": original.index("leftover@example.com") + len("leftover@example.com"),
                                "text": "leftover@example.com",
                                "score": 0.99,
                            }
                        ],
                    }
                ],
            }
        if url.endswith("/v1/chat/completions"):
            assert body["model"] == NEARAI_DEFAULT_MODEL
            content = body["messages"][0]["content"]
            assert placeholder in content
            assert "leftover@example.com" not in content
            assert "[REDACTED]" in content
            return {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}
        raise AssertionError(url)

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=True)
    turn = model.complete(messages=[{"role": "user", "content": original}], tools=[])
    assert turn.message == "ok"
    assert [url for url, _ in calls] == [
        "https://cloud-api.near.ai/v1/privacy/classify",
        "https://cloud-api.near.ai/v1/chat/completions",
    ]


def test_strip_thinking_keeps_only_the_answer() -> None:
    leaked = (
        "I could mention the unfinished Cancel thing. Keep short.</think>"
        "Not much — ready when you are!"
    )
    assert strip_thinking(leaked) == "Not much — ready when you are!"
    assert strip_thinking("<think>secret plan</think>\nHello") == "Hello"


def test_chat_model_strips_thinking_from_content() -> None:
    def transport(url: str, body: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": "<think>private monologue</think>Hi there",
                        "reasoning_content": "should never be shown",
                        "tool_calls": [],
                    }
                }
            ]
        }

    turn = ChatModel(transport=transport).complete(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert turn.message == "Hi there"
    assert "private" not in turn.message
    assert "should never" not in turn.message


def test_nearai_disables_thinking_on_chat_body() -> None:
    bodies: list[dict] = []

    def transport(url: str, body: dict) -> dict:
        bodies.append(body)
        return {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=False)
    model.complete(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert bodies[0]["chat_template_kwargs"]["enable_thinking"] is False
    assert bodies[0]["thinking"] == {"type": "disabled"}


def test_privacy_disabled_skips_classify() -> None:
    calls: list[str] = []

    def transport(url: str, body: dict) -> dict:
        calls.append(url)
        if url.endswith("/v1/privacy/classify"):
            raise AssertionError("privacy disabled")
        return {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=False)
    assert model.complete(messages=[{"role": "user", "content": "search leftover@example.com"}], tools=[]).message == "ok"
    assert calls == ["https://cloud-api.near.ai/v1/chat/completions"]


def test_privacy_classify_skips_system_and_caches_the_rest() -> None:
    classify_calls = 0
    chat_calls = 0
    system = "You help with groceries and reminders. " * 40  # would be costly if classified
    user = "buy milk for the house"

    def transport(url: str, body: dict) -> dict:
        nonlocal classify_calls, chat_calls
        if url.endswith("/v1/privacy/classify"):
            classify_calls += 1
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            assert system not in texts
            return {
                "data": [{"index": index, "spans": []} for index in range(len(texts))],
            }
        if url.endswith("/v1/chat/completions"):
            chat_calls += 1
            assert body["messages"][0]["content"] == system  # system passes through untouched
            return {"choices": [{"message": {"content": f"step-{chat_calls}", "tool_calls": []}}]}
        raise AssertionError(url)

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=True)
    first = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    assert model.complete(messages=first, tools=[]).message == "step-1"
    second = first + [
        {"role": "assistant", "content": "checking"},
        {"role": "tool", "tool_call_id": "c1", "content": "added milk to the list"},
    ]
    assert model.complete(messages=second, tools=[]).message == "step-2"
    # First step: classify user only. Second: classify new assistant+tool (batched), not system/user again.
    assert classify_calls == 2
    assert chat_calls == 2


def test_privacy_classify_batches_new_texts() -> None:
    calls: list[dict] = []

    def transport(url: str, body: dict) -> dict:
        if url.endswith("/v1/privacy/classify"):
            calls.append(body)
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return {"data": [{"index": i, "spans": []} for i in range(len(texts))]}
        return {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=True)
    model.complete(
        messages=[
            {"role": "system", "content": "System line one."},
            {"role": "user", "content": "User line two."},
            {"role": "tool", "tool_call_id": "1", "content": "Tool line three."},
        ],
        tools=[],
    )
    assert len(calls) == 1
    assert calls[0]["input"] == ["User line two.", "Tool line three."]


def test_placeholder_only_text_skips_privacy_classify() -> None:
    calls: list[str] = []
    placeholder = "[PERSON_deadbeefdeadbeefdeadbeefdeadbeef_1]"

    def transport(url: str, body: dict) -> dict:
        calls.append(url)
        if url.endswith("/v1/privacy/classify"):
            raise AssertionError("placeholder-only text should not be classified")
        return {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=True)
    turn = model.complete(
        messages=[
            {"role": "system", "content": "Huge trusted system prompt with many words."},
            {"role": "user", "content": f"Person: {placeholder}"},
        ],
        tools=[],
    )
    assert turn.message == "ok"
    assert calls == ["https://cloud-api.near.ai/v1/chat/completions"]


def test_privacy_classify_failure_is_fail_closed_no_chat() -> None:
    calls: list[str] = []

    def transport(url: str, body: dict) -> dict:
        calls.append(url)
        if url.endswith("/v1/privacy/classify"):
            raise HTTPError(url, 500, "boom", hdrs=None, fp=BytesIO(b'{"error":{"message":"down"}}'))
        raise AssertionError("chat must not run")

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=True)
    turn = model.complete(messages=[{"role": "user", "content": "hello"}], tools=[])
    assert "privacy filter" in turn.message.lower()
    assert calls
    assert all(url.endswith("/v1/privacy/classify") for url in calls)
    assert not any(url.endswith("/v1/chat/completions") for url in calls)


def test_privacy_classify_bad_payload_fail_closed() -> None:
    calls: list[str] = []

    def transport(url: str, body: dict) -> dict:
        calls.append(url)
        if url.endswith("/v1/privacy/classify"):
            return {"data": []}
        raise AssertionError("chat must not run")

    model = NearAiModel(transport=transport, api_key="sk-test", catalog=_tee_catalog(), privacy=True)
    turn = model.complete(messages=[{"role": "user", "content": "hello"}], tools=[])
    assert turn.message == "The privacy filter refused the request."
    assert calls == ["https://cloud-api.near.ai/v1/privacy/classify"]
