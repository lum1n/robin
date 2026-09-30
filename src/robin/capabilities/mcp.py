"""Attach MCP servers in chat. Secrets stay in the broker; tools become Robin tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from robin.capability import (
    Capability,
    Effect,
    FieldClass,
    FieldSpec,
    InputField,
    InputRequest,
    Registry,
    Result,
    SecretAccepted,
    Tool,
)
from robin.store import HouseholdStore

_SAFE_NAME = re.compile(r"[^a-z0-9_]+")
_MAX_MCP_TOOLS = 40
_STDIO_LAUNCHERS = frozenset({"npx", "uvx", "docker"})
_META_TOOLS = frozenset(
    {
        "mcp_setup_start",
        "mcp_setup_ask_secret",
        "mcp_setup_oauth",
        "mcp_setup_test",
        "mcp_setup_finish",
        "mcp_list",
        "mcp_tools",
        "mcp_remove",
    }
)


class SecretStore(Protocol):
    def put(self, account_id: str, name: str, value: str) -> None: ...
    def reveal(self, account_id: str, name: str) -> str: ...
    def delete(self, account_id: str, name: str) -> None: ...
    def names(self, account_id: str) -> list[str]: ...


class McpSession(Protocol):
    def list_tools(self) -> list[dict[str, Any]]: ...
    def call_tool(self, name: str, arguments: dict[str, Any]) -> str: ...


class Mcp(Capability):
    id = "mcp"
    tools = [
        Tool(
            name="mcp_setup_start",
            description=(
                "Start attaching an MCP server. transport is http or stdio. "
                "For http pass url; for stdio pass command and optional args (admins only). "
                "auth is none, bearer, or oauth (many remote MCPs such as Sentry use oauth)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "transport": {"type": "string", "enum": ["http", "stdio"]},
                    "url": {"type": "string"},
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "auth": {"type": "string", "enum": ["none", "bearer", "oauth"]},
                },
                "required": ["name", "transport"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="mcp_setup_ask_secret",
            description="Ask the person for a bearer token, header, or env secret for a draft MCP setup.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["bearer", "header", "env"]},
                    "key": {"type": "string"},
                },
                "required": ["name", "kind"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="mcp_setup_oauth",
            description=(
                "Start OAuth (authorization code + PKCE) for a draft HTTP MCP server. "
                "Opens a browser authorize link; after the person signs in, call mcp_setup_test if needed, then mcp_setup_finish."
            ),
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="mcp_setup_test",
            description="Test a draft MCP setup: initialize and list tools. Does not enable them yet. For oauth drafts, starts authorization if tokens are missing.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="mcp_setup_finish",
            description="Enable a tested MCP server. Lists tools for confirmation.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "enabled_tools": {"type": "array", "items": {"type": "string"}},
                    "trust": {"type": "string", "enum": ["ask", "auto"]},
                },
                "required": ["name"],
            },
            effect=Effect.MUTATE,
            confirm=True,
        ),
        Tool(
            name="mcp_list",
            description="List attached and draft MCP servers for this account.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="mcp_tools",
            description="Enable or disable tools on an attached MCP server (by remote tool name).",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "enable": {"type": "array", "items": {"type": "string"}},
                    "disable": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="mcp_remove",
            description="Remove an attached MCP server and its secrets.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            effect=Effect.MUTATE,
            confirm=True,
        ),
    ]
    fields = [
        FieldSpec("name", FieldClass.ORDINARY),
        FieldSpec("status", FieldClass.ORDINARY),
        FieldSpec("tools", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(
        self,
        *,
        broker: SecretStore | None = None,
        store: HouseholdStore | None = None,
        registry: Registry | None = None,
        open_session: Any = None,
        household: list[dict[str, Any]] | None = None,
        admins: set[str] | None = None,
        public_base: str = "",
    ) -> None:
        self.broker = broker
        self.store = store
        self.registry = registry
        self.open_session = open_session or _open_sdk_session
        self.household = household or []
        self.admins = admins or set()
        self.public_base = public_base.strip().rstrip("/")
        self._servers: dict[str, list[dict[str, Any]]] = {}
        self._drafts: dict[str, dict[str, dict[str, Any]]] = {}
        self._waits: dict[str, dict[str, Any]] = {}
        self._tool_index: dict[str, dict[str, tuple[str, str]]] = {}
        self._oauth_flows: dict[str, _OAuthFlow] = {}
        self._oauth_by_state: dict[str, _OAuthFlow] = {}
        if store is not None:
            self._servers = store.load_mcp()

    def summary(self, account_id: str) -> list[dict[str, Any]]:
        """Read-only list for GET /v1/mcp — names, status, and tool counts only."""
        rows: list[dict[str, Any]] = []
        for row in self._servers.get(account_id, []):
            enabled = row.get("enabled") or [str(tool.get("name") or "") for tool in row.get("tools") or []]
            rows.append(
                {
                    "name": str(row.get("name") or ""),
                    "status": str(row.get("status") or ""),
                    "transport": str(row.get("transport") or ""),
                    "tools": len(enabled),
                }
            )
        for name, draft in self._drafts.get(account_id, {}).items():
            rows.append(
                {
                    "name": name,
                    "status": "draft",
                    "transport": str(draft.get("transport") or ""),
                    "tools": len(draft.get("tools") or []),
                }
            )
        for row in self.household:
            visible = row.get("visible_to")
            if visible and account_id not in visible:
                continue
            rows.append(
                {
                    "name": str(row.get("name") or ""),
                    "status": "household",
                    "transport": str(row.get("transport") or ""),
                    "tools": len(row.get("tools") or row.get("enabled") or []),
                }
            )
        return rows

    def status(self, account_id: str) -> str:
        servers = self._servers.get(account_id, [])
        drafts = self._drafts.get(account_id, {})
        if not servers and not drafts:
            return ""
        active = sum(1 for row in servers if row.get("status") == "active")
        changed = sum(1 for row in servers if row.get("status") == "tools_changed")
        bits = [f"mcp: {active} active"]
        if drafts:
            bits.append(f"{len(drafts)} draft(s)")
        if changed:
            bits.append(f"{changed} need re-approve")
        return ", ".join(bits)

    def available_tools(self, account_id: str) -> list[Tool]:
        tools = list(self.tools)
        self._tool_index[account_id] = {}
        count = 0
        for row in self._servers.get(account_id, []):
            if row.get("status") != "active":
                continue
            enabled = set(row.get("enabled") or [])
            trust = str(row.get("trust") or "ask")
            remote = bool(row.get("transport") == "http")
            for tool in row.get("tools") or []:
                remote_name = str(tool.get("name") or "")
                if enabled and remote_name not in enabled:
                    continue
                if count >= _MAX_MCP_TOOLS:
                    break
                mapped = _tool_name(str(row.get("name") or "server"), remote_name)
                read_only = bool(tool.get("readOnlyHint"))
                destructive = bool(tool.get("destructiveHint"))
                confirm = destructive or (trust != "auto" and not read_only)
                effect = Effect.READ if read_only else Effect.MUTATE
                tools.append(
                    Tool(
                        name=mapped,
                        description=f"[MCP {row.get('name')}] {str(tool.get('description') or remote_name)[:400]}",
                        parameters=tool.get("inputSchema")
                        if isinstance(tool.get("inputSchema"), dict)
                        else {"type": "object", "properties": {}},
                        effect=effect,
                        confirm=confirm,
                        egress=remote and not read_only,
                        untrusted=True,
                    )
                )
                self._tool_index[account_id][mapped] = (str(row.get("name") or ""), remote_name)
                count += 1
        return tools

    def pending_input(self, account_id: str, conversation_id: str) -> InputRequest | None:
        waiting = self._waits.get(account_id)
        if waiting is None:
            return None
        kind = str(waiting.get("kind") or "bearer")
        if kind == "oauth":
            auth_url = str(waiting.get("auth_url") or "")
            name = waiting.get("name")
            error = str(waiting.get("error") or "").strip()
            if auth_url:
                reason = (
                    f"Sign in to connect MCP server {name}. "
                    f"Open this authorization link:\n{auth_url}\n"
                    "Approve access, then return here and submit. "
                    "If the browser does not return to Robin automatically, paste the final redirect URL."
                )
            else:
                reason = (
                    f"Sign in to connect MCP server {name}. "
                    "Open the authorization link, approve access, then return here and submit. "
                    "If the browser does not return to Robin automatically, paste the final redirect URL."
                )
            if error:
                reason = f"{error}\n\n{reason}"
            return InputRequest(
                request_id=str(waiting["request_id"]),
                title=f"Authorize MCP {name}",
                reason=reason,
                fields=(
                    InputField(
                        id="redirect",
                        label="Redirect URL (optional)",
                        kind="url",
                        required=False,
                        placeholder="https://…?code=…",
                    ),
                ),
                owner=self.id,
                open_url=auth_url,
            )
        label = "Access token" if kind == "bearer" else str(waiting.get("key") or "Secret")
        return InputRequest(
            request_id=str(waiting["request_id"]),
            title=f"MCP {waiting.get('name')} secret",
            reason=(
                f"Paste the {label} for MCP server {waiting.get('name')}. "
                "It is stored encrypted and not sent to the model."
            ),
            fields=(InputField(id="secret", label=label, kind="secret"),),
            owner=self.id,
        )

    def accept_input(
        self,
        account_id: str,
        conversation_id: str,
        request_id: str,
        values: dict[str, str],
        *,
        cancel: bool = False,
    ) -> SecretAccepted | None:
        waiting = self._waits.get(account_id)
        if waiting is None or waiting.get("request_id") != request_id:
            return None
        name = str(waiting.get("name") or "")
        kind = str(waiting.get("kind") or "bearer")
        if kind == "oauth":
            return self._accept_oauth(account_id, waiting, values, cancel=cancel)
        self._waits.pop(account_id, None)
        if cancel:
            return SecretAccepted(reply=f"MCP {name} secret cancelled.")
        secret = str(values.get("secret") or "").strip()
        if not secret:
            return SecretAccepted(reply="A secret is required.")
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is None:
            return SecretAccepted(reply="No draft for that MCP server.")
        key = str(waiting.get("key") or "")
        if kind == "bearer":
            draft["token"] = secret
        elif kind == "header":
            headers = dict(draft.get("headers") or {})
            headers[key or "Authorization"] = secret
            draft["headers"] = headers
        else:
            env = dict(draft.get("env") or {})
            env[key or "TOKEN"] = secret
            draft["env"] = env
        self._drafts.setdefault(account_id, {})[name] = draft
        return SecretAccepted(resume=f"Test the {name} MCP setup with mcp_setup_test.")

    def accept_secret(self, account_id: str, conversation_id: str, text: str) -> SecretAccepted | None:
        waiting = self._waits.get(account_id)
        if waiting is None:
            return None
        if text.strip().casefold() in {"cancel", "never mind", "nevermind"}:
            self._waits.pop(account_id, None)
            flow = self._oauth_flows.pop(account_id, None)
            if flow is not None:
                self._cancel_oauth(flow)
            return SecretAccepted(reply="Cancelled.")
        kind = str(waiting.get("kind") or "bearer")
        if kind == "oauth":
            return self.accept_input(
                account_id,
                conversation_id,
                str(waiting["request_id"]),
                {"redirect": text.strip()},
            )
        return self.accept_input(
            account_id,
            conversation_id,
            str(waiting["request_id"]),
            {"secret": text.strip()},
        )

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name in _META_TOOLS:
            return self._meta(account_id, tool_name, arguments)
        mapping = self._tool_index.get(account_id, {}).get(tool_name)
        if mapping is None:
            # Rebuild index from available_tools.
            self.available_tools(account_id)
            mapping = self._tool_index.get(account_id, {}).get(tool_name)
        if mapping is None:
            raise NotImplementedError(tool_name)
        server_name, remote_name = mapping
        row = next((item for item in self._servers.get(account_id, []) if item.get("name") == server_name), None)
        if row is None:
            return "MCP server not found"
        if row.get("status") == "tools_changed":
            return f"MCP {server_name} tools changed — re-approve with mcp_setup_test then mcp_setup_finish"
        session = self._session_for(account_id, row)
        try:
            live = session.list_tools()
        except Exception:
            live = None
        if live is not None and _tool_hash(live) != str(row.get("tool_hash") or ""):
            row["status"] = "tools_changed"
            self._save(account_id)
            return f"MCP {server_name} tools changed — re-approve with mcp_setup_test then mcp_setup_finish"
        return session.call_tool(remote_name, arguments)

    def _session_for(self, account_id: str, config: dict[str, Any]) -> McpSession:
        payload = dict(config)
        payload["_account_id"] = account_id
        payload["_broker"] = self.broker
        payload["_public_base"] = self.redirect_base()
        name = str(config.get("name") or "")
        if str(config.get("auth") or "") == "oauth" or self._has_oauth_tokens(account_id, name):
            payload["auth"] = "oauth"
            flow = self._oauth_flows.get(account_id)
            if flow is not None and flow.name == name and not flow.finished.is_set():
                payload["_oauth_flow"] = flow
        if self.broker is not None and not payload.get("token") and name:
            try:
                payload["token"] = self.broker.reveal(account_id, f"mcp:{name}:token")
            except Exception:
                pass
        return self.open_session(payload)

    def _meta(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "mcp_setup_start":
            return self._setup_start(account_id, arguments)
        if tool_name == "mcp_setup_ask_secret":
            return self._ask_secret(account_id, arguments)
        if tool_name == "mcp_setup_oauth":
            return self._setup_oauth(account_id, arguments)
        if tool_name == "mcp_setup_test":
            return self._setup_test(account_id, arguments)
        if tool_name == "mcp_setup_finish":
            return self._setup_finish(account_id, arguments)
        if tool_name == "mcp_list":
            return self._list(account_id)
        if tool_name == "mcp_tools":
            return self._toggle_tools(account_id, arguments)
        if tool_name == "mcp_remove":
            return self._remove(account_id, arguments)
        raise NotImplementedError(tool_name)

    def _setup_start(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        transport = str(arguments.get("transport") or "").strip()
        auth = str(arguments.get("auth") or "none").strip().casefold() or "none"
        if auth not in {"none", "bearer", "oauth"}:
            return "auth must be none, bearer, or oauth"
        if not name:
            return "name is required"
        if transport == "http":
            url = str(arguments.get("url") or "").strip()
            err = _validate_url(url)
            if err:
                return err
            if auth == "oauth":
                host = (urlparse(url).hostname or "").casefold()
                if not url.startswith("https://") and host not in {"localhost", "127.0.0.1", "::1"}:
                    return "oauth MCP url should be https (or localhost for development)"
            draft = {"name": name, "transport": "http", "url": url, "auth": auth, "status": "draft"}
        elif transport == "stdio":
            if account_id not in self.admins:
                return "stdio MCP servers can only be attached by household admins"
            if auth == "oauth":
                return "oauth is only for http MCP servers"
            command = str(arguments.get("command") or "").strip()
            if not _stdio_allowed(command):
                return "command must be npx, uvx, docker, or an absolute path under /opt/robin-mcp"
            args = [str(item) for item in (arguments.get("args") or [])]
            draft = {
                "name": name,
                "transport": "stdio",
                "command": command,
                "args": args,
                "auth": auth if auth != "oauth" else "none",
                "status": "draft",
            }
        else:
            return "transport must be http or stdio"
        self._drafts.setdefault(account_id, {})[name] = draft
        if auth == "oauth":
            return f"Draft MCP {name} saved (http, oauth). Call mcp_setup_oauth or mcp_setup_test to authorize."
        if auth == "bearer":
            return f"Draft MCP {name} saved ({transport}). Ask for the bearer secret, then mcp_setup_test."
        return f"Draft MCP {name} saved ({transport}). Ask for any secrets, then mcp_setup_test."

    def _ask_secret(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        kind = str(arguments.get("kind") or "bearer")
        key = str(arguments.get("key") or "")
        if name not in self._drafts.get(account_id, {}):
            return "no draft with that name — mcp_setup_start first"
        request_id = secrets.token_hex(8)
        self._waits[account_id] = {
            "request_id": request_id,
            "name": name,
            "kind": kind,
            "key": key,
        }
        return f"Ask the person for the {kind} secret for {name}."

    def _setup_oauth(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is None:
            return "no draft with that name — mcp_setup_start first"
        if draft.get("transport") != "http":
            return "oauth is only for http MCP servers"
        draft["auth"] = "oauth"
        return self._start_oauth(account_id, name)

    def _setup_test(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is None:
            return "no draft with that name"
        if str(draft.get("auth") or "") == "oauth" and not self._has_oauth_tokens(account_id, name):
            return self._start_oauth(account_id, name)
        try:
            session = self._session_for(account_id, draft)
            tools = session.list_tools()
        except Exception as exc:
            message = str(exc)
            if "401" in message or "Unauthorized" in message or "oauth" in message.casefold():
                draft["auth"] = "oauth"
                return self._start_oauth(account_id, name)
            return f"MCP test failed: {exc}"
        return self._record_test(account_id, name, draft, tools)

    def _start_oauth(self, account_id: str, name: str) -> str:
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is None:
            return "no draft with that name"
        existing = self._oauth_flows.get(account_id)
        if existing is not None and not existing.finished.is_set():
            if existing.name == name and existing.auth_url:
                self._waits[account_id] = {
                    "request_id": secrets.token_hex(8),
                    "name": name,
                    "kind": "oauth",
                    "auth_url": existing.auth_url,
                }
                return f"Authorize MCP {name} in the browser, then submit."
            return f"OAuth already in progress for {existing.name}"
        flow = _OAuthFlow(account_id=account_id, name=name)
        self._oauth_flows[account_id] = flow

        def worker() -> None:
            try:
                payload = dict(draft)
                payload["_account_id"] = account_id
                payload["_broker"] = self.broker
                payload["_public_base"] = self.redirect_base()
                payload["_oauth_flow"] = flow
                payload["auth"] = "oauth"
                payload["_oauth_register"] = lambda f: self._register_oauth_state(f)
                session = self.open_session(payload)
                tools = session.list_tools()
                draft["tools"] = tools
                draft["tool_hash"] = _tool_hash(tools)
                draft["tested"] = True
                draft["auth"] = "oauth"
                self._drafts.setdefault(account_id, {})[name] = draft
                flow.tools = tools
            except Exception as exc:  # noqa: BLE001 — surface to the waiting UI
                flow.result_error = exc
            finally:
                flow.finished.set()

        threading.Thread(target=worker, daemon=True, name=f"robin-mcp-oauth-{name}").start()
        waited = 0.0
        while waited < 90.0:
            if flow.auth_ready.wait(timeout=0.25):
                break
            if flow.finished.is_set():
                break
            waited += 0.25
        if flow.finished.is_set() and not flow.auth_url:
            self._oauth_flows.pop(account_id, None)
            if flow.result_error is not None:
                return f"OAuth failed: {flow.result_error}"
            tools = list(draft.get("tools") or flow.tools or [])
            if tools:
                return self._record_test(account_id, name, draft, tools)
            return f"MCP {name} authorized with no tools listed."
        if not flow.auth_url:
            self._cancel_oauth(flow)
            self._oauth_flows.pop(account_id, None)
            return "OAuth discovery timed out — check the MCP URL is reachable"
        if flow.state:
            self._oauth_by_state[flow.state] = flow
        request_id = secrets.token_hex(8)
        self._waits[account_id] = {
            "request_id": request_id,
            "name": name,
            "kind": "oauth",
            "auth_url": flow.auth_url,
        }
        return f"Authorize MCP {name} in the browser, then submit."

    def _accept_oauth(
        self,
        account_id: str,
        waiting: dict[str, Any],
        values: dict[str, str],
        *,
        cancel: bool,
    ) -> SecretAccepted:
        name = str(waiting.get("name") or "")
        flow = self._oauth_flows.get(account_id)
        if cancel:
            self._waits.pop(account_id, None)
            if flow is not None:
                self._cancel_oauth(flow)
                self._oauth_flows.pop(account_id, None)
            return SecretAccepted(reply=f"MCP {name} OAuth cancelled.")
        redirect = str(values.get("redirect") or "").strip()
        if redirect and flow is not None:
            try:
                code, state, iss = _parse_oauth_redirect(redirect)
            except ValueError as exc:
                waiting["error"] = str(exc)
                return SecretAccepted(reply=str(exc), input_again=True)
            waiting.pop("error", None)
            self.deliver_oauth_code(code, state, iss)
        elif flow is not None and flow.code is None and not flow.finished.is_set():
            flow.finished.wait(timeout=2.0)
            if flow.code is None and not flow.finished.is_set():
                message = (
                    "Still waiting for authorization. Open the link, approve access, "
                    "then submit again (paste the redirect URL if needed)."
                )
                waiting["error"] = message
                return SecretAccepted(reply=message, input_again=True)
        if flow is not None and not flow.finished.wait(timeout=60.0):
            message = "OAuth is still finishing — try again in a moment."
            waiting["error"] = message
            return SecretAccepted(reply=message, input_again=True)
        self._waits.pop(account_id, None)
        self._oauth_flows.pop(account_id, None)
        if flow is not None and flow.state:
            self._oauth_by_state.pop(flow.state, None)
        if flow is not None and flow.result_error is not None:
            return SecretAccepted(reply=f"OAuth failed: {flow.result_error}")
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is not None and draft.get("tested"):
            count = len(draft.get("tools") or [])
            return SecretAccepted(
                resume=f"OAuth for {name} succeeded ({count} tool(s)). Finish with mcp_setup_finish."
            )
        return SecretAccepted(resume=f"OAuth for {name} saved. Run mcp_setup_test, then mcp_setup_finish.")

    def _cancel_oauth(self, flow: _OAuthFlow) -> None:
        flow.cancelled = True
        if flow.state:
            self._oauth_by_state.pop(flow.state, None)
        self.deliver_oauth_code("", flow.state or "cancelled", None, error="cancelled")

    def _register_oauth_state(self, flow: _OAuthFlow) -> None:
        if flow.state:
            self._oauth_by_state[flow.state] = flow

    def deliver_oauth_code(
        self,
        code: str,
        state: str,
        iss: str | None,
        *,
        error: str | None = None,
    ) -> bool:
        flow = self._oauth_by_state.get(state) if state else None
        if flow is None:
            for candidate in self._oauth_flows.values():
                if not candidate.finished.is_set():
                    flow = candidate
                    break
        if flow is None:
            return False
        if error:
            flow.result_error = RuntimeError(error)
            flow.code = ""
            flow.callback_state = state
            flow.iss = iss
            flow.signal_code()
            return True
        if not code:
            return False
        flow.code = code
        flow.callback_state = state
        flow.iss = iss
        flow.signal_code()
        return True

    def complete_oauth_callback(self, query: dict[str, str]) -> tuple[int, bytes, str]:
        """Browser redirect target for MCP OAuth. No Robin session cookie required."""
        err = query.get("error") or ""
        if err:
            detail = query.get("error_description") or err
            body = (
                "<!doctype html><title>Robin MCP</title>"
                f"<p>Authorization failed: {_html(detail)}</p>"
                "<p>You can close this window.</p>"
            ).encode()
            return 400, body, "text/html; charset=utf-8"
        code = query.get("code") or ""
        state = query.get("state") or ""
        iss = query.get("iss") or None
        if not code or not state:
            body = b"<!doctype html><title>Robin MCP</title><p>Missing code or state.</p>"
            return 400, body, "text/html; charset=utf-8"
        if not self.deliver_oauth_code(code, state, iss):
            body = (
                b"<!doctype html><title>Robin MCP</title>"
                b"<p>No pending MCP authorization matched this callback.</p>"
            )
            return 404, body, "text/html; charset=utf-8"
        body = (
            b"<!doctype html><title>Robin MCP</title>"
            b"<p>Robin is connected. You can close this window and return to the app, then submit.</p>"
        )
        return 200, body, "text/html; charset=utf-8"

    def redirect_base(self) -> str:
        import os

        base = self.public_base or os.environ.get("ROBIN_PUBLIC_URL", "").strip().rstrip("/")
        if not base:
            base = "http://127.0.0.1:8787"
        return base

    def _has_oauth_tokens(self, account_id: str, name: str) -> bool:
        if self.broker is None:
            return False
        try:
            raw = self.broker.reveal(account_id, f"mcp:{name}:oauth_tokens")
        except Exception:
            return False
        return bool(raw and raw.strip() not in {"{}", "null"})

    def _record_test(
        self,
        account_id: str,
        name: str,
        draft: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> str:
        draft["tools"] = tools
        draft["tool_hash"] = _tool_hash(tools)
        draft["tested"] = True
        self._drafts.setdefault(account_id, {})[name] = draft
        for row in self._servers.get(account_id, []):
            if row.get("name") == name:
                row["tools"] = tools
                row["tool_hash"] = draft["tool_hash"]
                row["status"] = "active"
                self._save(account_id)
                break
        lines = [f"MCP {name} ok — {len(tools)} tool(s):"]
        for tool in tools[:30]:
            flags = []
            if tool.get("readOnlyHint"):
                flags.append("read")
            if tool.get("destructiveHint"):
                flags.append("destructive")
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"- {tool.get('name')}{suffix}: {str(tool.get('description') or '')[:120]}")
        return "\n".join(lines)

    def _setup_finish(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is None:
            existing = next((item for item in self._servers.get(account_id, []) if item.get("name") == name), None)
            if existing is not None and existing.get("status") == "active" and existing.get("tool_hash"):
                return (
                    f"MCP {name} already active with "
                    f"{len(existing.get('enabled') or existing.get('tools') or [])} tool(s)"
                )
            return "no draft with that name"
        if not draft.get("tested"):
            return "run mcp_setup_test successfully before finishing"
        tools = list(draft.get("tools") or [])
        enabled = arguments.get("enabled_tools")
        if isinstance(enabled, list) and enabled:
            wanted = {str(item) for item in enabled}
            tools_names = [str(tool.get("name") or "") for tool in tools if str(tool.get("name") or "") in wanted]
        else:
            tools_names = [str(tool.get("name") or "") for tool in tools]
        row = {
            "name": name,
            "transport": draft.get("transport"),
            "url": draft.get("url"),
            "command": draft.get("command"),
            "args": draft.get("args") or [],
            "headers": draft.get("headers") or {},
            "env": draft.get("env") or {},
            "token": draft.get("token") or "",
            "auth": str(draft.get("auth") or "none"),
            "tools": tools,
            "enabled": tools_names[:_MAX_MCP_TOOLS],
            "tool_hash": draft.get("tool_hash") or _tool_hash(tools),
            "trust": str(arguments.get("trust") or "ask"),
            "status": "active",
        }
        mapped = [_tool_name(name, remote) for remote in tools_names]
        if self.registry is not None:
            try:
                self.registry.claim_names(self.id, mapped)
            except ValueError as exc:
                return str(exc)
        rows = [item for item in self._servers.get(account_id, []) if item.get("name") != name]
        rows.append(row)
        self._servers[account_id] = rows
        self._drafts.get(account_id, {}).pop(name, None)
        self._save(account_id)
        if self.broker is not None and row.get("token"):
            self.broker.put(account_id, f"mcp:{name}:token", str(row["token"]))
        return f"activated MCP {name} with {len(tools_names)} tool(s)"

    def _list(self, account_id: str) -> str | Result:
        rows = []
        for row in self._servers.get(account_id, []):
            rows.append(
                {
                    "name": str(row.get("name") or ""),
                    "status": str(row.get("status") or ""),
                    "tools": str(len(row.get("enabled") or row.get("tools") or [])),
                }
            )
        for name, draft in self._drafts.get(account_id, {}).items():
            rows.append(
                {
                    "name": name,
                    "status": "draft",
                    "tools": "tested" if draft.get("tested") else "not tested",
                }
            )
        for row in self.household:
            if account_id in (row.get("visible_to") or [account_id]):
                rows.append(
                    {
                        "name": str(row.get("name") or ""),
                        "status": "household",
                        "tools": str(len(row.get("tools") or [])),
                    }
                )
        if not rows:
            return "No MCP servers."
        return Result(text="MCP servers:", records=rows)

    def _toggle_tools(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        rows = self._servers.get(account_id, [])
        row = next((item for item in rows if item.get("name") == name), None)
        if row is None:
            return "not found"
        enabled = set(row.get("enabled") or [str(t.get("name") or "") for t in row.get("tools") or []])
        for item in arguments.get("enable") or []:
            enabled.add(str(item))
        for item in arguments.get("disable") or []:
            enabled.discard(str(item))
        row["enabled"] = sorted(enabled)[:_MAX_MCP_TOOLS]
        self._save(account_id)
        return f"{name}: {len(row['enabled'])} tool(s) enabled"

    def _remove(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        before = self._servers.get(account_id, [])
        row = next((item for item in before if item.get("name") == name), None)
        kept = [item for item in before if item.get("name") != name]
        self._servers[account_id] = kept
        self._drafts.get(account_id, {}).pop(name, None)
        if row and self.registry is not None:
            mapped = [
                _tool_name(name, str(tool.get("name") or ""))
                for tool in row.get("tools") or []
            ]
            self.registry.release_names(self.id, mapped)
        if self.broker is not None:
            try:
                self.broker.delete(account_id, f"mcp:{name}:token")
            except Exception:
                pass
            for key in (f"mcp:{name}:oauth_tokens", f"mcp:{name}:oauth_client"):
                try:
                    self.broker.delete(account_id, key)
                except Exception:
                    pass
        self._save(account_id)
        return "removed" if row is not None else "not found"

    def _save(self, account_id: str) -> None:
        if self.store is not None:
            # Do not persist raw tokens in the mcp table when broker holds them.
            safe = []
            for row in self._servers.get(account_id, []):
                copy = dict(row)
                copy["token"] = ""
                safe.append(copy)
            self.store.save_mcp(account_id, safe)


def _sanitize(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name.strip().casefold()).strip("_")
    return cleaned[:40]


def _tool_name(server: str, remote: str) -> str:
    raw = f"mcp_{_sanitize(server)}_{_sanitize(remote)}"
    return raw[:64]


def _tool_hash(tools: list[dict[str, Any]]) -> str:
    blob = json.dumps(
        [{"name": t.get("name"), "description": t.get("description")} for t in tools],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _validate_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "url must be http or https"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "url is invalid"
    host = (parsed.hostname or "").casefold()
    if host in {"metadata.google.internal", "169.254.169.254"} or host.startswith("169.254."):
        return "url host is not allowed"
    return ""


def _stdio_allowed(command: str) -> bool:
    if command in _STDIO_LAUNCHERS:
        return True
    return command.startswith("/opt/robin-mcp/")


def _unavailable_session(config: dict[str, Any]) -> McpSession:
    raise RuntimeError("Robin MCP SDK missing — reinstall robin (mcp is a core dependency)")


class _AsyncLoop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="robin-mcp", daemon=True)
        self.thread.start()

    def run(self, coro: Any, timeout: float = 60.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


_LOOP: _AsyncLoop | None = None


def _mcp_loop() -> _AsyncLoop:
    global _LOOP
    if _LOOP is None:
        _LOOP = _AsyncLoop()
    return _LOOP


class _SdkSession:
    """Sync wrapper around the mcp Python SDK."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def list_tools(self) -> list[dict[str, Any]]:
        return _mcp_loop().run(self._list_tools())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return _mcp_loop().run(self._call_tool(name, arguments))

    async def _list_tools(self) -> list[dict[str, Any]]:
        async with self._session() as session:
            listed = await session.list_tools()
            rows: list[dict[str, Any]] = []
            for tool in listed.tools:
                annotations = getattr(tool, "annotations", None)
                rows.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema
                        if isinstance(getattr(tool, "inputSchema", None), dict)
                        else {"type": "object", "properties": {}},
                        "readOnlyHint": bool(getattr(annotations, "readOnlyHint", False)) if annotations else False,
                        "destructiveHint": bool(getattr(annotations, "destructiveHint", False))
                        if annotations
                        else False,
                    }
                )
            return rows

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        async with self._session() as session:
            result = await session.call_tool(name, arguments or {})
            parts: list[str] = []
            for block in getattr(result, "content", None) or []:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
                else:
                    parts.append(str(block))
            if getattr(result, "isError", False):
                return "MCP tool error: " + ("\n".join(parts) or "unknown")
            return "\n".join(parts) or "ok"

    def _session(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def opened():
            transport = str(self._config.get("transport") or "http")
            if transport == "http":
                async with self._http_session() as session:
                    yield session
            elif transport == "stdio":
                async with self._stdio_session() as session:
                    yield session
            else:
                raise RuntimeError(f"unsupported MCP transport {transport!r}")

        return opened()

    def _http_session(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def opened():
            from mcp import ClientSession

            try:
                from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            except ImportError:  # pragma: no cover — mcp 1.x
                from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

                create_mcp_http_client = None  # type: ignore[assignment]

            url = str(self._config.get("url") or "")
            headers = dict(self._config.get("headers") or {})
            token = str(self._config.get("token") or "")
            auth_mode = str(self._config.get("auth") or "")
            http_auth = None
            if auth_mode == "oauth":
                http_auth = _build_oauth_auth(self._config)
            elif token and "authorization" not in {key.casefold() for key in headers}:
                headers["Authorization"] = f"Bearer {token}"
            if create_mcp_http_client is not None:
                client = create_mcp_http_client(headers=headers or None, auth=http_auth)
                async with streamable_http_client(url, http_client=client) as streams:
                    read, write = streams[0], streams[1]
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            else:  # pragma: no cover
                if http_auth is not None:
                    raise RuntimeError("MCP OAuth requires mcp>=2.0")
                async with streamable_http_client(url, headers=headers or None) as streams:
                    read, write = streams[0], streams[1]
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session

        return opened()

    def _stdio_session(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def opened():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            command = str(self._config.get("command") or "")
            args = [str(item) for item in (self._config.get("args") or [])]
            env = {str(key): str(value) for key, value in (self._config.get("env") or {}).items()}
            account_id = str(self._config.get("_account_id") or "")
            if account_id:
                from robin.capabilities.identity import login_name

                login = login_name(account_id)
                params = StdioServerParameters(
                    command="runuser",
                    args=["--preserve-environment", "-u", login, "--", command, *args],
                    env=env or None,
                )
            else:
                params = StdioServerParameters(command=command, args=args, env=env or None)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        return opened()


def _open_sdk_session(config: dict[str, Any]) -> McpSession:
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Robin MCP SDK missing — reinstall robin (mcp is a core dependency)"
        ) from exc
    return _SdkSession(config)


def load_household_mcp(path: str) -> tuple[list[dict[str, Any]], set[str]]:
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError:
        return [], set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], set()
    servers = payload.get("servers") if isinstance(payload, dict) else payload
    admins = set(str(item) for item in (payload.get("admins") or []) if isinstance(payload, dict))
    if not isinstance(servers, list):
        return [], admins
    return [dict(item) for item in servers if isinstance(item, dict)], admins


class _OAuthFlow:
    def __init__(self, *, account_id: str, name: str) -> None:
        self.account_id = account_id
        self.name = name
        self.auth_url = ""
        self.state = ""
        self.code: str | None = None
        self.callback_state = ""
        self.iss: str | None = None
        self.auth_ready = threading.Event()
        self.finished = threading.Event()
        self.cancelled = False
        self.result_error: BaseException | None = None
        self.tools: list[dict[str, Any]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._code_event: asyncio.Event | None = None

    def bind_loop(self) -> asyncio.Event:
        self._loop = asyncio.get_running_loop()
        self._code_event = asyncio.Event()
        return self._code_event

    def signal_code(self) -> None:
        if self._loop is not None and self._code_event is not None:
            self._loop.call_soon_threadsafe(self._code_event.set)


class _BrokerTokenStorage:
    """Persist MCP OAuth tokens and dynamic client registration in the broker."""

    def __init__(self, broker: SecretStore, account_id: str, name: str) -> None:
        self.broker = broker
        self.account_id = account_id
        self.name = name

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        raw = self._reveal(f"mcp:{self.name}:oauth_tokens")
        if not raw:
            return None
        return OAuthToken.model_validate_json(raw)

    async def set_tokens(self, tokens: Any) -> None:
        self.broker.put(self.account_id, f"mcp:{self.name}:oauth_tokens", tokens.model_dump_json())

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._reveal(f"mcp:{self.name}:oauth_client")
        if not raw:
            return None
        return OAuthClientInformationFull.model_validate_json(raw)

    async def set_client_info(self, client_info: Any) -> None:
        self.broker.put(self.account_id, f"mcp:{self.name}:oauth_client", client_info.model_dump_json())

    def _reveal(self, key: str) -> str:
        try:
            return self.broker.reveal(self.account_id, key)
        except Exception:
            return ""


def _build_oauth_auth(config: dict[str, Any]) -> Any:
    from pydantic import AnyUrl

    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    broker = config.get("_broker")
    account_id = str(config.get("_account_id") or "")
    name = str(config.get("name") or "")
    if broker is None or not account_id or not name:
        raise RuntimeError("OAuth MCP requires a broker-backed account")
    flow: _OAuthFlow | None = config.get("_oauth_flow")
    base = str(config.get("_public_base") or "http://127.0.0.1:8787").rstrip("/")
    redirect = f"{base}/v1/mcp/oauth/callback"
    storage = _BrokerTokenStorage(broker, account_id, name)

    async def redirect_handler(authorization_url: str) -> None:
        if flow is None:
            raise RuntimeError("OAuth authorization required but no interactive flow is active")
        flow.auth_url = authorization_url
        params = parse_qs(urlparse(authorization_url).query)
        flow.state = (params.get("state") or [""])[0]
        register = config.get("_oauth_register")
        if callable(register):
            register(flow)
        flow.auth_ready.set()

    async def callback_handler() -> AuthorizationCodeResult:
        if flow is None:
            raise RuntimeError("OAuth authorization required but no interactive flow is active")
        event = flow.bind_loop()
        await event.wait()
        if flow.cancelled or flow.result_error is not None and not flow.code:
            raise RuntimeError(str(flow.result_error or "OAuth cancelled"))
        return AuthorizationCodeResult(
            code=str(flow.code or ""),
            state=flow.callback_state or flow.state or None,
            iss=flow.iss,
        )

    metadata = OAuthClientMetadata(
        client_name="Robin",
        redirect_uris=[AnyUrl(redirect)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    return OAuthClientProvider(
        server_url=str(config.get("url") or ""),
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def _parse_oauth_redirect(redirect: str) -> tuple[str, str, str | None]:
    parsed = urlparse(redirect.strip())
    params = parse_qs(parsed.query)
    if not params and parsed.fragment:
        params = parse_qs(parsed.fragment)
    if params.get("error"):
        raise ValueError(f"authorization error: {params['error'][0]}")
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    iss = (params.get("iss") or [None])[0]
    if not code or not state:
        raise ValueError("redirect URL must include code and state")
    return code, state, iss


def _html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
