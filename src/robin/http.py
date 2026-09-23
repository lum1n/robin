"""House-server HTTP API. Tests call dispatch and do not open a port."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robin.auth import Auth, AuthError
from robin.enroll import EnrollRejected, Enrollment
from robin.loop import PendingMissing, converse, resume
from robin.model import Model
from robin.policy import Task
from robin.provision import create_private_instance
from robin.session import Assistant


class Service:
    def __init__(
        self,
        assistant: Assistant,
        model: Model,
        *,
        enrollment: Enrollment | None = None,
        joint_url: str = "",
        exe_post: Any = None,
        auth: Auth | None = None,
    ) -> None:
        self.assistant = assistant
        self.model = model
        self.enrollment = enrollment
        self.joint_url = joint_url
        self.exe_post = exe_post
        self.auth = auth if auth is not None else Auth(assistant.store)


def dispatch(
    service: Service,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    query = query or {}
    body = body or {}
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    if method == "POST" and path == "/v1/accounts":
        return _post_account(service, body)
    if method == "POST" and path == "/v1/sessions":
        return _post_session(service, body)
    if method == "POST" and path == "/v1/messages":
        return _post_message(service, headers, body)
    if method == "POST" and path == "/v1/enroll":
        return _post_enroll(service, body)
    if method == "POST" and path == "/v1/secrets":
        return _post_secret(service, headers, body)
    if method == "GET" and path == "/v1/secrets":
        return _get_secrets(service, headers, query)
    if method == "POST" and path == "/v1/private":
        return _post_private(service, headers, body)
    if method == "GET" and path == "/v1/private":
        return _get_private(service, headers, query)
    if method == "GET" and path == "/v1/threads":
        denied = _require(service, headers, query.get("account_id"))
        if denied is not None:
            return denied
        return 200, {"threads": service.assistant.threads(query["account_id"])}
    if method == "GET" and path.startswith("/v1/threads/"):
        conversation_id = path.removeprefix("/v1/threads/")
        if not conversation_id or "/" in conversation_id:
            return 404, {"error": "not found"}
        denied = _require(service, headers, query.get("account_id"))
        if denied is not None:
            return denied
        return 200, {"turns": service.assistant.turns(query["account_id"], conversation_id)}
    if method == "GET" and path == "/v1/activity":
        denied = _require(service, headers, query.get("account_id"))
        if denied is not None:
            return denied
        return 200, {"entries": service.assistant.activity.read(query["account_id"])}
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
            headers = {key.lower(): value for key, value in self.headers.items()}
            status, response = dispatch(service, method, parsed.path, query=query, body=payload, headers=headers)
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _post_account(service: Service, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    password = body.get("password")
    if not isinstance(account_id, str) or not isinstance(password, str):
        return 400, {"error": "account_id and password are required"}
    try:
        service.auth.register(account_id, password)
    except AuthError as exc:
        if str(exc) == "account already has a password":
            return 409, {"error": "account already has a password"}
        return 400, {"error": "account and password are required"}
    return 201, {"account_id": account_id}


def _post_session(service: Service, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    password = body.get("password")
    if not isinstance(account_id, str) or not isinstance(password, str):
        return 400, {"error": "account_id and password are required"}
    try:
        token = service.auth.login(account_id, password)
    except AuthError:
        return 401, {"error": "login failed"}
    return 200, {"token": token}


def _post_message(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    conversation_id = body.get("conversation_id")
    if not isinstance(account_id, str) or not account_id or not isinstance(conversation_id, str) or not conversation_id:
        return 400, {"error": "account_id and conversation_id are required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
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


def _post_enroll(service: Service, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if service.enrollment is None:
        return 404, {"error": "not found"}
    token = body.get("token")
    if not isinstance(token, str) or not token:
        return 400, {"error": "token is required"}
    try:
        ready = service.enrollment.accept(token)
    except EnrollRejected:
        return 409, {"error": "token rejected"}
    return 200, {"account_id": ready["account_id"], "https_url": ready["https_url"], "ready": True}


_CONNECTABLE = ("calendar", "mailbox")


def _post_secret(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    name = body.get("name")
    value = body.get("value")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    if name not in _CONNECTABLE or not isinstance(value, str) or not value:
        return 400, {"error": "name and value are required"}
    service.assistant.broker.put(account_id, name, value)
    return 200, {"name": name, "connected": True}


def _get_secrets(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    denied = _require(service, headers, query.get("account_id"))
    if denied is not None:
        return denied
    names = [name for name in service.assistant.broker.names(query["account_id"]) if name in _CONNECTABLE]
    return 200, {"connected": names}


def _post_private(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    if service.enrollment is None or not service.joint_url or service.exe_post is None:
        return 400, {"error": "private instances are not configured"}
    confirmed = body.get("confirm") is True
    api_token = ""
    if confirmed:
        try:
            api_token = service.assistant.broker.reveal("household", "exe")
        except KeyError:
            return 400, {"error": "exe token is missing"}
    try:
        result = create_private_instance(
            enrollment=service.enrollment,
            account_id=account_id,
            confirmed=confirmed,
            joint_url=service.joint_url,
            post=service.exe_post,
            api_token=api_token,
        )
    except ValueError:
        return 400, {"error": "account id must be a lowercase slug"}
    except RuntimeError as exc:
        text = str(exc)
        if text in {"exe token is missing", "exe.dev did not return an https url"}:
            status = 400 if text == "exe token is missing" else 502
            return status, {"error": text}
        raise
    return 200, {"status": result.status, "https_url": result.https_url, "ready": result.ready}


def _get_private(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    account_id = query.get("account_id")
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    account_id = query["account_id"]
    if service.enrollment is None:
        return 404, {"error": "not found"}
    record = service.enrollment.get(account_id)
    if record is None:
        return 404, {"error": "not found"}
    return 200, {"https_url": record["https_url"], "ready": record["ready"]}


def _require(service: Service, headers: dict[str, str], account_id: str | None) -> tuple[int, dict[str, Any]] | None:
    if not account_id:
        return 400, {"error": "account_id is required"}
    actual = service.auth.account(_bearer(headers))
    if actual is None or actual != account_id:
        return 401, {"error": "login required"}
    return None


def _bearer(headers: dict[str, str]) -> str:
    header = headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token


def _public_reply(reply: Any) -> dict[str, Any]:
    return {
        "status": reply.status,
        "text": reply.text,
        "route": reply.route.value,
        "tool": reply.tool,
    }
