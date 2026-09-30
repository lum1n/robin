"""Attach MCP servers in chat. Secrets stay in the broker; tools become Robin tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
from typing import Any, Protocol
from urllib.parse import urlparse

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
                "For http pass url; for stdio pass command and optional args (admins only)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "transport": {"type": "string", "enum": ["http", "stdio"]},
                    "url": {"type": "string"},
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
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
            name="mcp_setup_test",
            description="Test a draft MCP setup: initialize and list tools. Does not enable them yet.",
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
    ) -> None:
        self.broker = broker
        self.store = store
        self.registry = registry
        self.open_session = open_session or _open_sdk_session
        self.household = household or []
        self.admins = admins or set()
        self._servers: dict[str, list[dict[str, Any]]] = {}
        self._drafts: dict[str, dict[str, dict[str, Any]]] = {}
        self._waits: dict[str, dict[str, Any]] = {}
        self._tool_index: dict[str, dict[str, tuple[str, str]]] = {}
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
        self._waits.pop(account_id, None)
        name = str(waiting.get("name") or "")
        if cancel:
            return SecretAccepted(reply=f"MCP {name} secret cancelled.")
        secret = str(values.get("secret") or "").strip()
        if not secret:
            return SecretAccepted(reply="A secret is required.")
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is None:
            return SecretAccepted(reply="No draft for that MCP server.")
        kind = str(waiting.get("kind") or "bearer")
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
            return SecretAccepted(reply="Cancelled.")
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
        return self.open_session(payload)

    def _meta(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "mcp_setup_start":
            return self._setup_start(account_id, arguments)
        if tool_name == "mcp_setup_ask_secret":
            return self._ask_secret(account_id, arguments)
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
        if not name:
            return "name is required"
        if transport == "http":
            url = str(arguments.get("url") or "").strip()
            err = _validate_url(url)
            if err:
                return err
            draft = {"name": name, "transport": "http", "url": url, "status": "draft"}
        elif transport == "stdio":
            if account_id not in self.admins:
                return "stdio MCP servers can only be attached by household admins"
            command = str(arguments.get("command") or "").strip()
            if not _stdio_allowed(command):
                return "command must be npx, uvx, docker, or an absolute path under /opt/robin-mcp"
            args = [str(item) for item in (arguments.get("args") or [])]
            draft = {
                "name": name,
                "transport": "stdio",
                "command": command,
                "args": args,
                "status": "draft",
            }
        else:
            return "transport must be http or stdio"
        self._drafts.setdefault(account_id, {})[name] = draft
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

    def _setup_test(self, account_id: str, arguments: dict[str, Any]) -> str:
        name = _sanitize(str(arguments.get("name") or ""))
        draft = self._drafts.get(account_id, {}).get(name)
        if draft is None:
            return "no draft with that name"
        try:
            session = self._session_for(account_id, draft)
            tools = session.list_tools()
        except Exception as exc:
            return f"MCP test failed: {exc}"
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
    raise RuntimeError("MCP client is not installed — pip install robin[mcp]")


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
    """Sync wrapper around the optional mcp Python SDK."""

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
            from mcp.client.streamable_http import streamablehttp_client

            url = str(self._config.get("url") or "")
            headers = dict(self._config.get("headers") or {})
            token = str(self._config.get("token") or "")
            if token and "authorization" not in {key.casefold() for key in headers}:
                headers["Authorization"] = f"Bearer {token}"
            async with streamablehttp_client(url, headers=headers or None) as streams:
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
        raise RuntimeError("MCP client is not installed — pip install robin[mcp]") from exc
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
