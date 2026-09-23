"""House-server HTTP API. Tests call dispatch and do not open a port."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robin.loop import PendingMissing, converse, resume
from robin.model import Model
from robin.policy import Task
from robin.session import Assistant


class Service:
    def __init__(self, assistant: Assistant, model: Model) -> None:
        self.assistant = assistant
        self.model = model


def dispatch(
    service: Service,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    query = query or {}
    body = body or {}
    if method == "POST" and path == "/v1/messages":
        return _post_message(service, body)
    if method == "GET" and path == "/v1/threads":
        account_id = query.get("account_id")
        if not account_id:
            return 400, {"error": "account_id is required"}
        return 200, {"threads": service.assistant.threads(account_id)}
    if method == "GET" and path.startswith("/v1/threads/"):
        conversation_id = path.removeprefix("/v1/threads/")
        if not conversation_id or "/" in conversation_id:
            return 404, {"error": "not found"}
        account_id = query.get("account_id")
        if not account_id:
            return 400, {"error": "account_id is required"}
        return 200, {"turns": service.assistant.turns(account_id, conversation_id)}
    if method == "GET" and path == "/v1/activity":
        account_id = query.get("account_id")
        if not account_id:
            return 400, {"error": "account_id is required"}
        return 200, {"entries": service.assistant.activity.read(account_id)}
    return 404, {"error": "not found"}


def serve(service: Service, host: str = "127.0.0.1", port: int = 8787) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def _respond(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode()) if raw else {}
            status, response = dispatch(service, method, parsed.path, query=query, body=payload)
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _post_message(service: Service, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    conversation_id = body.get("conversation_id")
    if not isinstance(account_id, str) or not account_id or not isinstance(conversation_id, str) or not conversation_id:
        return 400, {"error": "account_id and conversation_id are required"}
    if body.get("confirm") is True:
        try:
            reply = resume(service.assistant, account_id, conversation_id)
        except PendingMissing:
            return 409, {"error": "nothing to confirm"}
        return 200, _public_reply(reply)
    service.assistant.clear_pending(account_id, conversation_id)
    reply = converse(
        service.assistant,
        Task(
            account_id=account_id,
            conversation_id=conversation_id,
            text=str(body.get("text") or ""),
            allow_cloud=bool(body.get("allow_cloud")),
            free_text=bool(body.get("free_text")),
        ),
        service.model,
    )
    if reply.status == "confirm" and reply.tool:
        service.assistant.set_pending(
            account_id,
            conversation_id,
            reply.tool,
            reply.arguments or {},
            reply.route.value,
        )
    return 200, _public_reply(reply)


def _public_reply(reply: Any) -> dict[str, Any]:
    return {
        "status": reply.status,
        "text": reply.text,
        "route": reply.route.value,
        "tool": reply.tool,
    }
