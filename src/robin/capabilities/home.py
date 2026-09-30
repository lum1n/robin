"""Home Assistant connector. Locks, alarms, and garage doors wait for confirmation."""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from robin.capability import (
    Capability,
    DueWork,
    Effect,
    FieldClass,
    FieldSpec,
    InputField,
    InputRequest,
    Result,
    SecretAccepted,
    Tool,
)
from robin.store import HouseholdStore

_EXTERNAL_DOMAINS = frozenset({"lock", "alarm_control_panel", "siren"})
_ATTR_KEYS = (
    "brightness",
    "brightness_pct",
    "color_temp_kelvin",
    "rgb_color",
    "temperature",
    "current_temperature",
    "hvac_mode",
    "volume_level",
    "media_title",
    "media_artist",
    "position",
    "current_position",
    "device_class",
    "friendly_name",
    "area_id",
)
_ON_STATES = frozenset({"on", "open", "home", "unlocked", "playing"})


class SecretStore(Protocol):
    def put(self, account_id: str, name: str, value: str) -> None: ...
    def reveal(self, account_id: str, name: str) -> str: ...
    def delete(self, account_id: str, name: str) -> None: ...
    def names(self, account_id: str) -> list[str]: ...


class Home(Capability):
    id = "home"
    tools = [
        Tool(
            name="home_connect",
            description=(
                "Start connecting Home Assistant. Pass the base URL (http://…:8123). "
                "Robin then asks for a long-lived access token through a secure form."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="home_connect_test",
            description="Test the pending or saved Home Assistant connection and report version and entity count.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="home_disconnect",
            description="Forget this account's Home Assistant connection.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.MUTATE,
            confirm=True,
        ),
        Tool(
            name="home_list",
            description="List Home Assistant entities. Optional area, domain, or free-text query.",
            parameters={
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "domain": {"type": "string"},
                    "query": {"type": "string"},
                },
            },
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
                "Set lights, climate, covers, switches, and similar. "
                "Optional brightness_pct, color_temp_kelvin, rgb, temperature, hvac_mode, position, volume. "
                "For locks, alarms, and garage doors use home_secure."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "state": {"type": "string"},
                    "brightness_pct": {"type": "number"},
                    "color_temp_kelvin": {"type": "number"},
                    "rgb": {"type": "array", "items": {"type": "number"}},
                    "temperature": {"type": "number"},
                    "hvac_mode": {"type": "string"},
                    "position": {"type": "number"},
                    "volume": {"type": "number"},
                },
                "required": ["entity_id"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="home_secure",
            description="Set a lock, alarm, siren, or garage/gate cover. Waits for confirmation.",
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
        Tool(
            name="home_scene",
            description="Activate a Home Assistant scene by entity_id.",
            parameters={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="home_script",
            description="Run a Home Assistant script by entity_id.",
            parameters={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="home_media",
            description=(
                "Control a media_player: play, pause, next, previous, volume, or play_media. "
                "For play_media, pass a search query when the player supports browse_media."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "next", "previous", "volume", "play_media"],
                    },
                    "volume": {"type": "number"},
                    "query": {"type": "string"},
                },
                "required": ["entity_id", "action"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="home_rule_add",
            description=(
                "When an entity changes to a state (optionally from another), run an action sentence. "
                "Example: person.vegard to not_home → turn off all lights."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "to_state": {"type": "string"},
                    "from_state": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["entity_id", "to_state", "action"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="home_rule_list",
            description="List this account's Home Assistant presence/state rules.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="home_rule_cancel",
            description="Cancel a Home Assistant rule by id.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("entity_id", FieldClass.ORDINARY),
        FieldSpec("state", FieldClass.ORDINARY),
        FieldSpec("name", FieldClass.ORDINARY, free_text=True),
        FieldSpec("area", FieldClass.ORDINARY, free_text=True),
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("action", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(
        self,
        *,
        broker: SecretStore | None = None,
        store: HouseholdStore | None = None,
        call: Any = None,
    ) -> None:
        self.broker = broker
        self.store = store
        self._call = call or ha_call
        self._areas: dict[str, tuple[float, dict[str, str]]] = {}
        self._drafts: dict[str, dict[str, str]] = {}
        self._waits: dict[str, dict[str, Any]] = {}
        self._rules: dict[str, list[dict[str, Any]]] = {}
        self._last: dict[str, dict[str, str]] = {}
        if store is not None:
            self._rules = store.load_home_rules()
            self._last = store.load_home_state()

    def status(self, account_id: str) -> str:
        if self._creds(account_id) is None:
            if account_id in self._drafts:
                return "homeassistant: setup in progress"
            return "homeassistant: not connected — ask Robin to connect it"
        return "homeassistant: connected"

    def available_tools(self, account_id: str) -> list[Tool]:
        if self._creds(account_id) is None and account_id not in self._drafts:
            return [tool for tool in self.tools if tool.name.startswith("home_connect")]
        return list(self.tools)

    def pending_input(self, account_id: str, conversation_id: str) -> InputRequest | None:
        waiting = self._waits.get(account_id)
        if waiting is None:
            return None
        return InputRequest(
            request_id=str(waiting["request_id"]),
            title="Home Assistant token",
            reason="Paste a long-lived access token from Home Assistant. It is stored encrypted and not sent to the model.",
            fields=(InputField(id="token", label="Access token", kind="secret"),),
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
        if cancel:
            self._drafts.pop(account_id, None)
            return SecretAccepted(reply="Home Assistant setup cancelled.")
        token = str(values.get("token") or "").strip()
        if not token:
            return SecretAccepted(reply="A token is required.")
        draft = self._drafts.get(account_id) or {}
        draft["token"] = token
        self._drafts[account_id] = draft
        return SecretAccepted(resume="Test the Home Assistant connection with home_connect_test.")

    def accept_secret(self, account_id: str, conversation_id: str, text: str) -> SecretAccepted | None:
        waiting = self._waits.get(account_id)
        if waiting is None:
            return None
        lowered = text.strip().casefold()
        if lowered in {"cancel", "never mind", "nevermind"}:
            self._waits.pop(account_id, None)
            self._drafts.pop(account_id, None)
            return SecretAccepted(reply="Home Assistant setup cancelled.")
        token = text.strip()
        if not token or " " in token and len(token) < 20:
            return None
        self._waits.pop(account_id, None)
        draft = self._drafts.get(account_id) or {}
        draft["token"] = token
        self._drafts[account_id] = draft
        return SecretAccepted(resume="Test the Home Assistant connection with home_connect_test.")

    def due(self, now: datetime) -> list[DueWork]:
        work: list[DueWork] = []
        for account_id, rules in list(self._rules.items()):
            if not rules or self._creds(account_id) is None:
                continue
            last = self._last.setdefault(account_id, {})
            for rule in rules:
                entity_id = str(rule.get("entity_id") or "")
                if not entity_id:
                    continue
                try:
                    item = self._call(self._creds(account_id), "GET", f"/api/states/{entity_id}")
                    state = str(item.get("state") or "")
                except Exception:
                    continue
                previous = last.get(entity_id, "")
                last[entity_id] = state
                to_state = str(rule.get("to_state") or "")
                from_state = str(rule.get("from_state") or "")
                if state != to_state:
                    continue
                if from_state and previous != from_state:
                    continue
                if previous == state:
                    continue
                action = str(rule.get("action") or "").strip()
                if not action:
                    continue
                rule_id = str(rule.get("id") or "")

                def finish(result: str, account: str = account_id) -> None:
                    self._save_last(account)

                work.append(
                    DueWork(
                        account_id=account_id,
                        conversation_id="home",
                        text=action,
                        finish=finish,
                    )
                )
                # Avoid re-firing immediately: mark last as already at to_state after scheduling.
                last[entity_id] = state
            self._save_last(account_id)
        return work

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "home_connect":
            return self._connect(account_id, arguments)
        if tool_name == "home_connect_test":
            return self._connect_test(account_id)
        if tool_name == "home_disconnect":
            return self._disconnect(account_id)
        creds = self._creds(account_id)
        if creds is None:
            return "Home Assistant is not connected. Use home_connect with the URL first."
        if tool_name == "home_list":
            return self._list(account_id, creds, arguments)
        if tool_name == "home_state":
            return self._state(creds, arguments)
        if tool_name == "home_set":
            return self._set(creds, arguments, secure=False)
        if tool_name == "home_secure":
            return self._set(creds, arguments, secure=True)
        if tool_name == "home_scene":
            entity_id = str(arguments.get("entity_id") or "")
            self._call(creds, "POST", "/api/services/scene/turn_on", {"entity_id": entity_id})
            return f"activated {entity_id}"
        if tool_name == "home_script":
            entity_id = str(arguments.get("entity_id") or "")
            domain, _, name = entity_id.partition(".")
            if domain != "script" or not name:
                return "entity_id must be script.name"
            self._call(creds, "POST", f"/api/services/script/{name}", {})
            return f"ran {entity_id}"
        if tool_name == "home_media":
            return self._media(creds, arguments)
        if tool_name == "home_rule_add":
            return self._rule_add(account_id, arguments)
        if tool_name == "home_rule_list":
            rows = self._rules.get(account_id, [])
            if not rows:
                return "No rules."
            return Result(
                text="Rules:",
                records=[
                    {
                        "id": str(row.get("id") or ""),
                        "entity_id": str(row.get("entity_id") or ""),
                        "state": f"{row.get('from_state') or '*'} → {row.get('to_state') or ''}",
                        "action": str(row.get("action") or ""),
                    }
                    for row in rows
                ],
            )
        if tool_name == "home_rule_cancel":
            rule_id = str(arguments.get("id") or "")
            before = self._rules.get(account_id, [])
            kept = [row for row in before if row.get("id") != rule_id]
            self._rules[account_id] = kept
            self._save_rules(account_id)
            return "cancelled" if len(kept) != len(before) else "not found"
        raise NotImplementedError(tool_name)

    def _connect(self, account_id: str, arguments: dict[str, Any]) -> str:
        url = str(arguments.get("url") or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            return "url must be http or https"
        self._drafts[account_id] = {"url": url}
        request_id = secrets.token_hex(8)
        self._waits[account_id] = {"request_id": request_id, "url": url}
        return (
            f"Home Assistant URL saved as {url}. "
            "Ask for the long-lived access token next — Robin will show a secure form."
        )

    def _connect_test(self, account_id: str) -> str:
        draft = self._drafts.get(account_id)
        creds = None
        if draft and draft.get("url") and draft.get("token"):
            creds = {"url": draft["url"], "token": draft["token"]}
        elif self._creds(account_id) is not None:
            creds = self._creds(account_id)
        if creds is None:
            return "No pending or saved Home Assistant connection to test."
        try:
            config = self._call(creds, "GET", "/api/config")
            states = self._call(creds, "GET", "/api/states")
        except Exception as exc:
            return f"Home Assistant test failed: {exc}"
        version = str(config.get("version") or "unknown")
        count = len(states) if isinstance(states, list) else 0
        if draft and draft.get("token"):
            if self.broker is not None:
                self.broker.put(
                    account_id,
                    "homeassistant",
                    json.dumps({"url": draft["url"], "token": draft["token"]}, sort_keys=True),
                )
            self._drafts.pop(account_id, None)
            return f"Connected to Home Assistant {version} with {count} entities."
        return f"Home Assistant {version} reachable with {count} entities."

    def _disconnect(self, account_id: str) -> str:
        self._drafts.pop(account_id, None)
        if self.broker is not None:
            try:
                self.broker.delete(account_id, "homeassistant")
            except Exception:
                pass
        return "disconnected"

    def _list(self, account_id: str, creds: dict[str, str], arguments: dict[str, Any]) -> Result | str:
        rows = self._call(creds, "GET", "/api/states")
        if not isinstance(rows, list):
            return "could not list entities"
        area_filter = str(arguments.get("area") or "").casefold()
        domain_filter = str(arguments.get("domain") or "").casefold()
        query = str(arguments.get("query") or "").casefold()
        areas = self._area_map(account_id, creds)
        records = []
        for item in rows:
            entity_id = str(item.get("entity_id") or "")
            domain = entity_id.split(".", 1)[0]
            attrs = item.get("attributes") or {}
            name = str(attrs.get("friendly_name") or "")
            area = areas.get(entity_id, "")
            if domain_filter and domain != domain_filter:
                continue
            if area_filter and area_filter not in area.casefold():
                continue
            blob = " ".join([entity_id, name, area, str(item.get("state") or "")]).casefold()
            if query and query not in blob:
                continue
            row = {
                "entity_id": entity_id,
                "state": str(item.get("state") or ""),
                "name": name,
                "area": area,
            }
            for key in _ATTR_KEYS:
                if key in attrs and key not in {"friendly_name", "area_id"}:
                    row[key] = str(attrs[key])
            records.append(row)
            if len(records) >= 80:
                break
        if not records:
            return "No entities matched."
        return Result(text="Entities:", records=records)

    def _state(self, creds: dict[str, str], arguments: dict[str, Any]) -> Result:
        entity_id = str(arguments.get("entity_id") or "")
        item = self._call(creds, "GET", f"/api/states/{entity_id}")
        attrs = item.get("attributes") or {}
        row = {
            "entity_id": str(item.get("entity_id") or entity_id),
            "state": str(item.get("state") or ""),
            "name": str(attrs.get("friendly_name") or ""),
        }
        for key in _ATTR_KEYS:
            if key in attrs:
                row[key] = str(attrs[key])
        return Result(records=[row])

    def _set(self, creds: dict[str, str], arguments: dict[str, Any], *, secure: bool) -> str:
        entity_id = str(arguments.get("entity_id") or "")
        state = str(arguments.get("state") or "").strip()
        domain = entity_id.split(".", 1)[0]
        device_class = ""
        try:
            item = self._call(creds, "GET", f"/api/states/{entity_id}")
            device_class = str((item.get("attributes") or {}).get("device_class") or "")
        except Exception:
            pass
        garage = domain == "cover" and device_class in {"garage", "gate"}
        if not secure and (domain in _EXTERNAL_DOMAINS or garage):
            return "Use home_secure for locks, alarms, sirens, and garage/gate covers."
        if secure and domain not in _EXTERNAL_DOMAINS and not garage:
            return "home_secure is only for locks, alarms, sirens, and garage/gate covers."
        service, data = _service_for(domain, entity_id, state, arguments, device_class=device_class)
        self._call(creds, "POST", f"/api/services/{domain}/{service}", data)
        return f"set {entity_id}"

    def _media(self, creds: dict[str, str], arguments: dict[str, Any]) -> str:
        entity_id = str(arguments.get("entity_id") or "")
        action = str(arguments.get("action") or "").strip()
        if not entity_id.startswith("media_player."):
            return "entity_id must be a media_player"
        mapping = {
            "play": "media_play",
            "pause": "media_pause",
            "next": "media_next_track",
            "previous": "media_previous_track",
        }
        if action in mapping:
            self._call(creds, "POST", f"/api/services/media_player/{mapping[action]}", {"entity_id": entity_id})
            return f"{action} {entity_id}"
        if action == "volume":
            volume = float(arguments.get("volume") or 0)
            self._call(
                creds,
                "POST",
                "/api/services/media_player/volume_set",
                {"entity_id": entity_id, "volume_level": max(0.0, min(1.0, volume))},
            )
            return f"volume {entity_id}"
        if action == "play_media":
            query = str(arguments.get("query") or "").strip()
            if not query:
                return "query is required for play_media"
            self._call(
                creds,
                "POST",
                "/api/services/media_player/play_media",
                {
                    "entity_id": entity_id,
                    "media_content_id": query,
                    "media_content_type": "music",
                },
            )
            return f"play_media {entity_id}: {query}"
        return f"unknown action {action}"

    def _rule_add(self, account_id: str, arguments: dict[str, Any]) -> str:
        entity_id = str(arguments.get("entity_id") or "").strip()
        to_state = str(arguments.get("to_state") or "").strip()
        action = str(arguments.get("action") or "").strip()
        if not entity_id or not to_state or not action:
            return "entity_id, to_state, and action are required"
        row = {
            "id": secrets.token_hex(4),
            "entity_id": entity_id,
            "to_state": to_state,
            "from_state": str(arguments.get("from_state") or "").strip(),
            "action": action[:500],
        }
        self._rules.setdefault(account_id, []).append(row)
        self._save_rules(account_id)
        return f"rule {row['id']} saved"

    def _area_map(self, account_id: str, creds: dict[str, str]) -> dict[str, str]:
        cached = self._areas.get(account_id)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        mapping: dict[str, str] = {}
        try:
            raw = self._call(
                creds,
                "POST",
                "/api/template",
                {
                    "template": (
                        "{% for area in areas() %}"
                        "{{ area }}|{% for e in area_entities(area) %}{{ e }},{% endfor %};"
                        "{% endfor %}"
                    )
                },
            )
            text = raw if isinstance(raw, str) else str(raw)
            for chunk in text.split(";"):
                if "|" not in chunk:
                    continue
                area, _, entities = chunk.partition("|")
                for entity_id in entities.split(","):
                    entity_id = entity_id.strip()
                    if entity_id:
                        mapping[entity_id] = area.strip()
        except Exception:
            mapping = {}
        self._areas[account_id] = (time.time(), mapping)
        return mapping

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

    def _save_rules(self, account_id: str) -> None:
        if self.store is not None:
            self.store.save_home_rules(account_id, self._rules.get(account_id, []))

    def _save_last(self, account_id: str) -> None:
        if self.store is not None:
            self.store.save_home_state(account_id, self._last.get(account_id, {}))


def _service_for(
    domain: str,
    entity_id: str,
    state: str,
    arguments: dict[str, Any],
    *,
    device_class: str = "",
) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {"entity_id": entity_id}
    if domain == "lock":
        return ("unlock" if state in _ON_STATES else "lock"), data
    if domain == "alarm_control_panel":
        return ("alarm_disarm" if "disarm" in state.casefold() else "alarm_arm_home"), data
    if domain == "siren":
        return ("turn_on" if state in _ON_STATES else "turn_off"), data
    if domain == "cover":
        if "position" in arguments and arguments["position"] is not None:
            data["position"] = int(arguments["position"])
            return "set_cover_position", data
        if state in _ON_STATES or state == "open":
            return "open_cover", data
        return "close_cover", data
    if domain == "climate":
        if arguments.get("temperature") is not None:
            data["temperature"] = float(arguments["temperature"])
            return "set_temperature", data
        if arguments.get("hvac_mode"):
            data["hvac_mode"] = str(arguments["hvac_mode"])
            return "set_hvac_mode", data
        return ("turn_on" if state in _ON_STATES else "turn_off"), data
    if domain == "light":
        if arguments.get("brightness_pct") is not None:
            data["brightness_pct"] = float(arguments["brightness_pct"])
        if arguments.get("color_temp_kelvin") is not None:
            data["color_temp_kelvin"] = int(arguments["color_temp_kelvin"])
        if arguments.get("rgb"):
            data["rgb_color"] = [int(v) for v in arguments["rgb"][:3]]
        if any(key in data for key in ("brightness_pct", "color_temp_kelvin", "rgb_color")) or state in _ON_STATES:
            return "turn_on", data
        return "turn_off", data
    if domain == "media_player" and arguments.get("volume") is not None:
        data["volume_level"] = float(arguments["volume"])
        return "volume_set", data
    if state in _ON_STATES:
        return "turn_on", data
    if state:
        return "turn_off", data
    return "toggle", data


def ha_call(creds: dict[str, str], method: str, path: str, body: dict | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        creds["url"] + path,
        data=data,
        headers={"Authorization": f"Bearer {creds['token']}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310
            raw = response.read().decode()
    except HTTPError as exc:
        detail = exc.read().decode() if hasattr(exc, "read") else str(exc)
        raise RuntimeError(f"HTTP {exc.code}: {detail[:200]}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    if path == "/api/template":
        return raw
    return json.loads(raw) if raw else {}
