"""Ask the person for non-secret details through the app input form."""

from __future__ import annotations

import json
import secrets
from typing import Any

from robin.capability import (
    Capability,
    Effect,
    FieldClass,
    FieldSpec,
    InputField,
    InputRequest,
    Result,
    SecretAccepted,
    Tool,
    current_task,
)

_SAFE_KINDS = frozenset({"text", "url", "email", "username", "number", "choice"})


class Ask(Capability):
    id = "ask"
    tools = [
        Tool(
            name="ask_person",
            description=(
                "Ask the person for non-secret details through a form in the app "
                "(text, url, email, username, number, or a choice). "
                "Do not use this for passwords, tokens, or one-time codes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "kind": {
                                    "type": "string",
                                    "enum": ["text", "url", "email", "username", "number", "choice"],
                                },
                                "required": {"type": "boolean"},
                                "placeholder": {"type": "string"},
                                "options": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["id", "label"],
                        },
                    },
                },
                "required": ["title", "fields"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("label", FieldClass.ORDINARY, free_text=True),
        FieldSpec("value", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], InputRequest] = {}
        self._answers: dict[str, dict[str, str]] = {}

    def pending_input(self, account_id: str, conversation_id: str) -> InputRequest | None:
        return self._pending.get((account_id, conversation_id))

    def accept_input(
        self,
        account_id: str,
        conversation_id: str,
        request_id: str,
        values: dict[str, str],
        *,
        cancel: bool = False,
    ) -> SecretAccepted | None:
        key = (account_id, conversation_id)
        request = self._pending.get(key)
        if request is None or request.request_id != request_id:
            return None
        self._pending.pop(key, None)
        if cancel:
            self._answers[request_id] = {"_cancelled": "1"}
            return SecretAccepted(reply="Cancelled.")
        cleaned = {str(k): str(v).strip() for k, v in values.items() if str(v).strip()}
        self._answers[request_id] = cleaned
        labels = ", ".join(field.label for field in request.fields if field.id in cleaned) or "details"
        lines = [f"{field.label}: {cleaned[field.id]}" for field in request.fields if field.id in cleaned]
        return SecretAccepted(
            reply="",
            resume="The person answered:\n" + "\n".join(lines) if lines else f"The person provided: {labels}.",
        )

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name != "ask_person":
            raise NotImplementedError(tool_name)
        title = str(arguments.get("title") or "").strip() or "Robin needs a detail"
        reason = str(arguments.get("reason") or title).strip()
        raw_fields = arguments.get("fields") or []
        if not isinstance(raw_fields, list) or not raw_fields:
            return "fields are required"
        built: list[InputField] = []
        for item in raw_fields[:8]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "text").strip().lower()
            if kind not in _SAFE_KINDS:
                return f"ask_person cannot request kind {kind!r}; only non-secret fields are allowed"
            field_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if not field_id or not label:
                continue
            options = tuple(str(opt) for opt in (item.get("options") or []) if str(opt).strip())
            built.append(
                InputField(
                    id=field_id[:64],
                    label=label[:120],
                    kind=kind,
                    required=bool(item.get("required", True)),
                    placeholder=str(item.get("placeholder") or "")[:120],
                    options=options[:20],
                )
            )
        if not built:
            return "fields are required"
        turn = current_task.get()
        conversation_id = turn.conversation_id if turn is not None else ""
        request_id = secrets.token_hex(8)
        request = InputRequest(
            request_id=request_id,
            title=title[:120],
            reason=reason[:400],
            fields=tuple(built),
            owner=self.id,
        )
        self._pending[(account_id, conversation_id)] = request
        return f"INPUT_REQUEST:{request_id}"

    def take_answer(self, request_id: str) -> dict[str, str] | None:
        return self._answers.pop(request_id, None)
