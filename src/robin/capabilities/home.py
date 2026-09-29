"""Home Assistant connector. Locks and alarms wait for confirmation."""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.request import Request, urlopen

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool

_EXTERNAL_DOMAINS = {"lock", "alarm_control_panel"}


class SecretStore(Protocol):
    def reveal(self, account_id: str, name: str) -> str: ...


class Home(Capability):
    id = "home"
    tools = [
        Tool(
            name="home_list",
            description="List Home Assistant entities this account can see.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="home_state",
            description="Get the state of one Home Assistant entity_id.",
            parameters={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="home_set",
            description=(
                "Set a Home Assistant entity. Lights and climate run immediately. "
                "Locks and alarms wait for confirmation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "state": {"type": "string"},
                },
                "required": ["entity_id", "state"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("entity_id", FieldClass.ORDINARY),
        FieldSpec("state", FieldClass.ORDINARY),
        FieldSpec("name", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, *, broker: SecretStore | None = None, call: Any = None) -> None:
        self.broker = broker
        self._call = call or ha_call

    def status(self, account_id: str) -> str:
        if self._creds(account_id) is None:
            return "homeassistant: not connected"
        return "homeassistant: connected"

    def available_tools(self, account_id: str) -> list[Tool]:
        if self._creds(account_id) is None:
            return []
        return list(self.tools)

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        creds = self._creds(account_id)
        if creds is None:
            return "Home Assistant is not connected."
        if tool_name == "home_list":
            rows = self._call(creds, "GET", "/api/states")
            records = [
                {
                    "entity_id": str(item.get("entity_id") or ""),
                    "state": str(item.get("state") or ""),
                    "name": str(((item.get("attributes") or {}).get("friendly_name")) or ""),
                }
                for item in rows[:100]
            ]
            return Result(text="Entities:", records=records)
        if tool_name == "home_state":
            entity_id = str(arguments.get("entity_id", ""))
            item = self._call(creds, "GET", f"/api/states/{entity_id}")
            return Result(
                records=[
                    {
                        "entity_id": str(item.get("entity_id") or entity_id),
                        "state": str(item.get("state") or ""),
                        "name": str(((item.get("attributes") or {}).get("friendly_name")) or ""),
                    }
                ]
            )
        if tool_name == "home_set":
            entity_id = str(arguments.get("entity_id", ""))
            state = str(arguments.get("state", ""))
            domain = entity_id.split(".", 1)[0]
            service = "turn_on" if state in {"on", "open", "home", "unlocked"} else "turn_off"
            if domain == "lock":
                service = "unlock" if state in {"unlocked", "open", "on"} else "lock"
            if domain == "alarm_control_panel":
                service = "alarm_disarm" if "disarm" in state else "alarm_arm_home"
            self._call(
                creds,
                "POST",
                f"/api/services/{domain}/{service}",
                {"entity_id": entity_id},
            )
            return f"set {entity_id}"
        raise NotImplementedError(tool_name)

    def _creds(self, account_id: str) -> dict[str, str] | None:
        if self.broker is None:
            return None
        try:
            raw = self.broker.reveal(account_id, "homeassistant")
        except KeyError:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        url, token = parsed.get("url"), parsed.get("token")
        if not isinstance(url, str) or not isinstance(token, str) or not url or not token:
            return None
        return {"url": url.rstrip("/"), "token": token}


# Declare external effect for locks/alarms by wrapping invoke through a custom Tool list at runtime.
# The registry uses the static tools list; session.invoke checks effect. Override resolve via a second tool.
Home.tools = [
    Tool(
        name="home_list",
        description="List Home Assistant entities this account can see.",
        parameters={"type": "object", "properties": {}},
        effect=Effect.READ,
    ),
    Tool(
        name="home_state",
        description="Get the state of one Home Assistant entity_id.",
        parameters={
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
        effect=Effect.READ,
    ),
    Tool(
        name="home_set",
        description=(
            "Set lights or climate. For locks and alarms use home_secure instead so the person can confirm."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "state": {"type": "string"},
            },
            "required": ["entity_id", "state"],
        },
        effect=Effect.MUTATE,
    ),
    Tool(
        name="home_secure",
        description="Set a lock or alarm. Waits for confirmation.",
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "state": {"type": "string"},
            },
            "required": ["entity_id", "state"],
        },
        effect=Effect.EXTERNAL,
    ),
]


def _patch_invoke() -> None:
    original = Home.invoke

    def invoke(self: Home, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "home_secure":
            return original(self, account_id, "home_set", arguments)
        if tool_name == "home_set":
            entity_id = str(arguments.get("entity_id", ""))
            domain = entity_id.split(".", 1)[0]
            if domain in _EXTERNAL_DOMAINS:
                return "Use home_secure for locks and alarms."
        return original(self, account_id, tool_name, arguments)

    Home.invoke = invoke  # type: ignore[method-assign]


_patch_invoke()


def ha_call(creds: dict[str, str], method: str, path: str, body: dict | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        creds["url"] + path,
        data=data,
        headers={"Authorization": f"Bearer {creds['token']}", "Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310
        raw = response.read().decode()
    return json.loads(raw) if raw else {}
