"""House-server HTTP API. Tests call dispatch and do not open a port."""

from __future__ import annotations

import json
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from robin.auth import Auth, AuthError
from robin.enroll import EnrollRejected, Enrollment
from robin.loop import PendingMissing, converse, resume
from robin.model import Model
from robin.policy import Task
from robin.provision import create_private_instance, delete_private_instance
from robin.session import Assistant
from robin.vault import VaultAccessError


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
    if method == "POST" and path == "/v1/schedule":
        return _post_schedule(service, headers, body)
    if method == "GET" and path == "/v1/schedule":
        return _get_schedule(service, headers, query)
    if method == "POST" and path == "/v1/export":
        return _post_export(service, headers, body)
    if method == "POST" and path == "/v1/import":
        return _post_import(service, headers, body)
    if method == "POST" and path == "/v1/secrets":
        return _post_secret(service, headers, body)
    if method == "GET" and path == "/v1/secrets":
        return _get_secrets(service, headers, query)
    if method == "POST" and path == "/v1/profile":
        return _post_profile(service, headers, body)
    if method == "GET" and path == "/v1/profile":
        return _get_profile(service, headers, query)
    if method == "GET" and path == "/v1/mcp":
        return _get_mcp(service, headers, query)
    if method == "GET" and path == "/v1/status":
        return _get_status(service, headers, query)
    if method == "GET" and path == "/v1/mcp/oauth/callback":
        return _mcp_oauth_callback_json(service, query)
    if method == "GET" and path == "/v1/notifications":
        return _get_notifications(service, headers, query)
    if method == "POST" and path == "/v1/notifications":
        return _post_notifications(service, headers, body)
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
    if method == "DELETE" and path.startswith("/v1/threads/"):
        conversation_id = path.removeprefix("/v1/threads/")
        if not conversation_id or "/" in conversation_id:
            return 404, {"error": "not found"}
        denied = _require(service, headers, query.get("account_id"))
        if denied is not None:
            return denied
        deleted = service.assistant.delete_thread(query["account_id"], conversation_id)
        if not deleted:
            return 404, {"error": "not found"}
        return 200, {"ok": True}
    if method == "GET" and path == "/v1/activity":
        denied = _require(service, headers, query.get("account_id"))
        if denied is not None:
            return denied
        return 200, {"entries": service.assistant.activity.read(query["account_id"])}
    if method == "GET" and path == "/v1/browser/live":
        return _get_browser_live(service, headers, query)
    if method == "GET" and path == "/v1/browser/frame":
        return _get_browser_frame(service, headers, query)
    return 404, {"error": "not found"}


# Client closed the socket while we were still writing (common on long browser turns).
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
# Phone/NAT paths often drop sockets that stay quiet for ~60–90s while a turn runs.
_HEARTBEAT_SECONDS = 5.0
_HEARTBEAT_GRACE_SECONDS = 1.0


def _arm_keepalive(connection: socket.socket | None) -> None:
    if connection is None:
        return
    try:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20)
        if hasattr(socket, "TCP_KEEPINTVL"):
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        if hasattr(socket, "TCP_KEEPCNT"):
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
    except OSError:
        return


def _write_chunk(handler: BaseHTTPRequestHandler, data: bytes) -> None:
    handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
    handler.wfile.flush()


def _send(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
    """Write an HTTP response; ignore disconnects from the peer."""
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        if content_type != "application/json":
            handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
    except _CLIENT_GONE:
        return


def _send_json_with_heartbeat(
    handler: BaseHTTPRequestHandler,
    compute: Callable[[], tuple[int, dict[str, Any]]],
) -> None:
    """Run compute(); if it takes long, keep the socket warm with chunked whitespace.

    JSON allows leading whitespace, so phone clients can decode the final object.
    Status is frozen at 200 once heartbeats start (auth failures return sooner).
    Requires HTTP/1.1 — chunked transfer is not valid on HTTP/1.0.
    """
    done = threading.Event()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = compute()
        except Exception:
            traceback.print_exc()
            box["result"] = (500, {"error": "request failed"})
        finally:
            done.set()

    worker = threading.Thread(target=run, name="robin-http-turn", daemon=True)
    worker.start()
    if done.wait(_HEARTBEAT_GRACE_SECONDS):
        status, response = box["result"]
        _send(handler, status, json.dumps(response).encode(), "application/json")
        return

    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.flush()
        # Immediate first byte so the phone's idle timer resets before the first wait.
        _write_chunk(handler, b"\n")
    except _CLIENT_GONE:
        done.wait()
        return

    while not done.wait(_HEARTBEAT_SECONDS):
        try:
            _write_chunk(handler, b"\n")
        except _CLIENT_GONE:
            done.wait()
            return

    status, response = box["result"]
    payload = response if status == 200 else {"error": response.get("error", "request failed")}
    try:
        _write_chunk(handler, json.dumps(payload).encode())
        _write_chunk(handler, b"")
    except _CLIENT_GONE:
        return


def serve(service: Service, host: str = "127.0.0.1", port: int = 8787) -> None:
    class Handler(BaseHTTPRequestHandler):
        # Chunked heartbeats for long /v1/messages turns require HTTP/1.1.
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            _arm_keepalive(getattr(self, "connection", None))

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            headers = {key.lower(): value for key, value in self.headers.items()}
            if parsed.path == "/v1/browser/live":
                status, body, content_type = _browser_live_page(service, headers, query)
                _send(self, status, body, content_type)
                return
            if parsed.path == "/v1/browser/frame":
                status, body, content_type = _browser_frame_bytes(service, headers, query)
                _send(self, status, body, content_type)
                return
            if parsed.path == "/v1/mcp/oauth/callback":
                status, body, content_type = _mcp_oauth_callback(service, query)
                _send(self, status, body, content_type)
                return
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def _respond(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode()) if raw else {}
            except json.JSONDecodeError:
                _send(self, 400, json.dumps({"error": "json is required"}).encode(), "application/json")
                return
            headers = {key.lower(): value for key, value in self.headers.items()}

            def compute() -> tuple[int, dict[str, Any]]:
                try:
                    return dispatch(service, method, parsed.path, query=query, body=payload, headers=headers)
                except Exception:
                    traceback.print_exc()
                    return 500, {"error": "request failed"}

            if method == "POST" and parsed.path == "/v1/messages":
                _send_json_with_heartbeat(self, compute)
                return
            status, response = compute()
            _send(self, status, json.dumps(response).encode(), "application/json")

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
            reply = resume(service.assistant, account_id, conversation_id, service.model)
        except PendingMissing:
            return 409, {"error": "nothing to confirm"}
        return 200, _public_reply(reply)
    incoming = body.get("input")
    if isinstance(incoming, dict) and incoming.get("request_id"):
        return _post_input(service, account_id, conversation_id, incoming, body)
    service.assistant.clear_pending(account_id, conversation_id)
    ner = service.assistant.ner
    if getattr(ner, "_installed", False) and not ner.available():
        return 200, {
            "status": "reply",
            "text": "Robin is starting.",
            "route": "local",
            "timing": "",
        }
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
    return 200, _public_reply(reply)


def _post_input(
    service: Service,
    account_id: str,
    conversation_id: str,
    incoming: dict[str, Any],
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    request_id = str(incoming.get("request_id") or "")
    cancel = incoming.get("cancel") is True
    raw_values = incoming.get("values") if isinstance(incoming.get("values"), dict) else {}
    values = {str(key): str(value) for key, value in raw_values.items()}
    pending = service.assistant.pending_input(account_id, conversation_id)
    if pending is None or pending.request_id != request_id:
        return 409, {"error": "nothing to fill in"}
    labels = [field.label for field in pending.fields if field.id in values and str(values[field.id]).strip()]
    accepted = service.assistant.accept_input(
        account_id, conversation_id, request_id, values, cancel=cancel
    )
    if accepted is None:
        return 409, {"error": "nothing to fill in"}
    if labels and not cancel and service.assistant.store is not None:
        service.assistant.store.append_turn(
            account_id,
            conversation_id,
            "user",
            "provided: " + ", ".join(labels),
        )
    if accepted.reply and not accepted.resume:
        if accepted.input_again:
            again = service.assistant.pending_input(account_id, conversation_id)
            if again is not None:
                return 200, {
                    "status": "input",
                    "text": again.reason or accepted.reply,
                    "route": "local",
                    "tool": None,
                    "timing": "",
                    "input": again.public(),
                }
        return 200, {
            "status": "reply",
            "text": accepted.reply,
            "route": "local",
            "timing": "",
        }
    reply = converse(
        service.assistant,
        Task(
            account_id=account_id,
            conversation_id=conversation_id,
            text=accepted.resume or "continue",
            allow_cloud=bool(body.get("allow_cloud", accepted.allow_cloud)),
            free_text=bool(body.get("free_text", accepted.free_text)),
        ),
        service.model,
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


def _post_schedule(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    if not isinstance(body.get("enabled"), bool):
        return 400, {"error": "enabled is required"}
    service.assistant.set_schedule(account_id, body["enabled"])
    return 200, {"enabled": body["enabled"]}


def _get_schedule(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    denied = _require(service, headers, query.get("account_id"))
    if denied is not None:
        return denied
    return 200, {"enabled": service.assistant.schedule_enabled(query["account_id"])}


def _notify(service: Service):
    for capability in service.assistant.registry._capabilities:
        if getattr(capability, "id", "") == "notify" and hasattr(capability, "pending"):
            return capability
    return None


def _get_notifications(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    account_id = query.get("account_id")
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    notify = _notify(service)
    items = notify.pending(account_id) if notify is not None else []
    return 200, {"notifications": items}


def _post_notifications(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    raw = body.get("ack")
    if not isinstance(raw, list):
        return 400, {"error": "ack is required"}
    ids = [str(item) for item in raw if str(item)]
    notify = _notify(service)
    cleared = notify.ack(account_id, ids) if notify is not None and hasattr(notify, "ack") else 0
    return 200, {"acked": cleared}


def _post_export(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    passphrase = body.get("passphrase")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    if not isinstance(passphrase, str) or not passphrase:
        return 400, {"error": "passphrase is required"}
    try:
        blob = service.assistant.export_account(account_id, passphrase)
    except ValueError:
        return 400, {"error": "passphrase is required"}
    return 200, {"export": blob}


def _post_import(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    passphrase = body.get("passphrase")
    blob = body.get("export")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    if not isinstance(passphrase, str) or not passphrase or not isinstance(blob, str) or not blob:
        return 400, {"error": "passphrase and export are required"}
    try:
        count = service.assistant.import_account(account_id, passphrase, blob)
    except VaultAccessError as exc:
        text = str(exc)
        if passphrase in text:
            text = "export rejected"
        return 400, {"error": text}
    return 200, {"imported": count}


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


def _post_profile(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from robin.profile import PROFILE_FIELDS, filled_keys

    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    fields = body.get("fields")
    if not isinstance(fields, dict):
        return 400, {"error": "fields are required"}
    updates = {key: fields[key] for key in PROFILE_FIELDS if key in fields}
    if not updates and fields:
        return 400, {"error": "fields are required"}
    saved = service.assistant.set_profile(account_id, updates)
    return 200, {"fields": saved, "present": filled_keys(saved)}


def _get_profile(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    from robin.profile import filled_keys

    account_id = query.get("account_id")
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    fields = service.assistant.get_profile(account_id)
    return 200, {"fields": fields, "present": filled_keys(fields)}


def _get_mcp(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    account_id = query.get("account_id")
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    servers: list[dict[str, Any]] = []
    for capability in service.assistant.registry.for_account(account_id or ""):
        summary = getattr(capability, "summary", None)
        if callable(summary) and getattr(capability, "id", "") == "mcp":
            servers = summary(account_id)
            break
    return 200, {"servers": servers}


def _get_status(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """One-shot overview for RobinKit: connectors, MCP, capabilities, schedule, threads."""
    account_id = query.get("account_id")
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    assert isinstance(account_id, str)
    body = service.assistant.overview(account_id)
    private: dict[str, Any] | None = None
    if service.enrollment is not None:
        record = service.enrollment.get(account_id)
        if record is not None:
            private = {"https_url": record["https_url"], "ready": record["ready"]}
    body["private"] = private
    return 200, body


def _mcp_capability(service: Service) -> Any:
    for capability in service.assistant.registry._capabilities:
        if getattr(capability, "id", "") == "mcp":
            return capability
    return None


def _mcp_oauth_callback(service: Service, query: dict[str, str]) -> tuple[int, bytes, str]:
    capability = _mcp_capability(service)
    if capability is None or not callable(getattr(capability, "complete_oauth_callback", None)):
        return 404, b"<!doctype html><p>MCP is not enabled.</p>", "text/html; charset=utf-8"
    return capability.complete_oauth_callback(query)


def _mcp_oauth_callback_json(service: Service, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    status, body, _content_type = _mcp_oauth_callback(service, query)
    if status == 200:
        return 200, {"ok": True}
    return status, {"error": body.decode("utf-8", errors="replace")[:200]}


def _post_private(service: Service, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        return 400, {"error": "account_id is required"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied
    if service.enrollment is None or not service.joint_url or service.exe_post is None:
        return 400, {"error": "private instances are not configured"}
    if body.get("delete") is True:
        return _delete_private(service, account_id, body)
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


def _delete_private(service: Service, account_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if service.enrollment is None or service.exe_post is None:
        return 400, {"error": "private instances are not configured"}
    confirmed = body.get("confirm") is True
    api_token = ""
    if confirmed:
        try:
            api_token = service.assistant.broker.reveal("household", "exe")
        except KeyError:
            return 400, {"error": "exe token is missing"}
    try:
        result = delete_private_instance(
            enrollment=service.enrollment,
            account_id=account_id,
            confirmed=confirmed,
            post=service.exe_post,
            api_token=api_token,
        )
    except LookupError:
        return 404, {"error": "not found"}
    except ValueError:
        return 400, {"error": "account id must be a lowercase slug"}
    except RuntimeError as exc:
        text = str(exc)
        if text == "exe token is missing":
            return 400, {"error": text}
        if text == "exe.dev delete failed":
            return 502, {"error": text}
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


def _get_browser_live(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """JSON metadata for tests and clients that do not want the HTML page."""
    denied = _require(service, headers, query.get("account_id"))
    if denied is not None:
        return denied
    account_id = query["account_id"]
    return 200, {
        "account_id": account_id,
        "live_path": f"/v1/browser/live?account_id={account_id}",
        "frame_path": f"/v1/browser/frame?account_id={account_id}",
    }


def _get_browser_frame(service: Service, headers: dict[str, str], query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    denied = _require(service, headers, query.get("account_id"))
    if denied is not None:
        return denied
    return 200, {"ok": True}


def _browser_capability(service: Service, account_id: str) -> Any:
    for capability in service.assistant.registry.for_account(account_id):
        if getattr(capability, "id", "") == "display" and hasattr(capability, "screenshot"):
            return capability
    return None


def _browser_live_page(
    service: Service, headers: dict[str, str], query: dict[str, str]
) -> tuple[int, bytes, str]:
    denied = _require(service, headers, query.get("account_id"))
    if denied is not None:
        return denied[0], json.dumps(denied[1]).encode(), "application/json"
    account_id = query["account_id"]
    token = _bearer(headers)
    # Token in query lets a WebView load the img without custom headers.
    frame = f"/v1/browser/frame?account_id={account_id}&access_token={token}"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Robin live view</title>
<meta http-equiv="refresh" content="2">
<style>
body{{margin:0;background:#111;color:#eee;font:14px system-ui,sans-serif}}
img{{max-width:100%;height:auto;display:block;margin:0 auto}}
p{{padding:12px}}
</style></head><body>
<p>Solve any captcha or security check here, then tap Done in Robin.</p>
<img src="{frame}" alt="live page">
</body></html>
"""
    return 200, html.encode(), "text/html; charset=utf-8"


def _browser_frame_bytes(
    service: Service, headers: dict[str, str], query: dict[str, str]
) -> tuple[int, bytes, str]:
    account_id = query.get("account_id")
    # Prefer Authorization; fall back to access_token for <img> loads.
    if not headers.get("authorization") and query.get("access_token"):
        headers = {**headers, "authorization": f"Bearer {query['access_token']}"}
    denied = _require(service, headers, account_id)
    if denied is not None:
        return denied[0], json.dumps(denied[1]).encode(), "application/json"
    assert account_id is not None
    browser = _browser_capability(service, account_id)
    if browser is None:
        return 404, b"no browser", "text/plain"
    try:
        png = browser.screenshot(account_id)
    except Exception as exc:
        return 409, str(exc).encode(), "text/plain"
    return 200, png, "image/png"


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
    payload = {
        "status": reply.status,
        "text": reply.text,
        "route": reply.route.value,
        "tool": reply.tool,
        "timing": getattr(reply, "timing", "") or "",
    }
    live = getattr(reply, "live_url", "") or ""
    if live:
        payload["live_url"] = live
    request = getattr(reply, "input", None)
    if request is not None and hasattr(request, "public"):
        payload["input"] = request.public()
    return payload
