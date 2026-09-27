"""Desktop apps on this instance via AT-SPI. Same text refs as the browser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

from robin.capabilities.browser import _format_snapshot, _wants_page
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool

_MAX_INTERACTIVE = 120
_MAX_DEPTH = 40
_MAX_NODES = 2500

_DESKTOP_WORDS = re.compile(
    r"\b("
    r"desktop|window|application|calculator|gedit|nautilus|files app|"
    r"libreoffice|terminal emulator|settings|gnome|kde|"
    r"open the app|in the app|on the desktop|native app"
    r")\b",
    re.IGNORECASE,
)

_SENSITIVE = re.compile(
    r"\b(send|pay|payment|purchase|buy|delete|remove|trash|empty trash|uninstall)\b",
    re.IGNORECASE,
)


class Surface(Protocol):
    def snapshot(self, app: str = "") -> dict[str, Any]: ...

    def click(self, ref: str) -> None: ...

    def type_text(self, ref: str, text: str) -> None: ...

    def press_key(self, key: str) -> None: ...

    def focus_window(self, index: int) -> None: ...

    def available(self) -> bool: ...


@dataclass
class MemoryControl:
    role: str
    name: str
    region: str = "window"
    states: list[str] = field(default_factory=list)
    value: str = ""
    actions: list[str] = field(default_factory=lambda: ["click"])


class MemorySurface:
    """In-memory desktop for tests. Same snapshot shape as AT-SPI."""

    def __init__(
        self,
        *,
        app: str = "Calculator",
        title: str = "Calculator",
        controls: list[MemoryControl] | None = None,
        content: str = "",
    ) -> None:
        self.app = app
        self.title = title
        self.controls = list(controls or [])
        self.content = content
        self.windows = [{"index": 1, "url": f"desktop://{quote(app, safe='')}", "title": title, "active": True}]
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.keys: list[str] = []
        self.focused = 1
        self._alive = True

    def available(self) -> bool:
        return self._alive

    def snapshot(self, app: str = "") -> dict[str, Any]:
        label = app.strip() or self.app
        url = f"desktop://{quote(label, safe='')}"
        interactive: list[dict[str, Any]] = []
        for index, control in enumerate(self.controls, start=1):
            interactive.append(
                {
                    "ref": str(index),
                    "role": control.role,
                    "name": control.name,
                    "region": control.region,
                    "states": list(control.states),
                    "value": control.value,
                }
            )
        return {
            "url": url,
            "title": self.title,
            "pages": list(self.windows),
            "interactive": interactive[:_MAX_INTERACTIVE],
            "content": self.content,
            "more_below": False,
        }

    def click(self, ref: str) -> None:
        self._require(ref)
        self.clicked.append(ref)

    def type_text(self, ref: str, text: str) -> None:
        control = self._require(ref)
        control.value = text
        if "focused" not in control.states:
            control.states.append("focused")
        self.typed.append((ref, text))

    def press_key(self, key: str) -> None:
        self.keys.append(key)

    def focus_window(self, index: int) -> None:
        if index < 1 or index > len(self.windows):
            raise RuntimeError(f"no window {index}")
        self.focused = index
        for window in self.windows:
            window["active"] = window["index"] == index

    def _require(self, ref: str) -> MemoryControl:
        try:
            position = int(str(ref).strip()) - 1
        except ValueError as exc:
            raise RuntimeError(f"unknown ref {ref}") from exc
        if position < 0 or position >= len(self.controls):
            raise RuntimeError(f"unknown ref {ref}")
        return self.controls[position]


class AtspiSurface:
    """Live AT-SPI tree. Missing bus or bindings become a clear error string."""

    def __init__(self) -> None:
        self._refs: dict[str, Any] = {}
        self._windows: list[Any] = []
        self._active = 0

    def available(self) -> bool:
        try:
            return self._desktop() is not None
        except Exception:
            return False

    def snapshot(self, app: str = "") -> dict[str, Any]:
        atspi, desktop = self._load()
        root = self._pick_window(desktop, app)
        if root is None:
            return {
                "url": "desktop://",
                "title": "",
                "pages": [],
                "interactive": [],
                "content": "(no accessible window)",
                "more_below": False,
            }
        app_name = _node_name(root) or app or "desktop"
        title = _window_title(root) or app_name
        interactive, content = _walk(root, atspi)
        self._refs = {str(item["ref"]): item["_node"] for item in interactive}
        for item in interactive:
            item.pop("_node", None)
        pages = self._list_windows(desktop, root)
        return {
            "url": f"desktop://{quote(app_name, safe='')}",
            "title": title,
            "pages": pages,
            "interactive": interactive[:_MAX_INTERACTIVE],
            "content": content,
            "more_below": False,
        }

    def click(self, ref: str) -> None:
        node = self._ref(ref)
        if _do_action(node, ("click", "press", "activate", "Jump")):
            return
        raise RuntimeError(f"cannot click ref {ref}")

    def type_text(self, ref: str, text: str) -> None:
        node = self._ref(ref)
        editable = getattr(node, "get_editable_text_iface", lambda: None)()
        if editable is not None:
            try:
                editable.set_text_contents(text)
                return
            except Exception:
                pass
        text_iface = getattr(node, "get_text_iface", lambda: None)()
        if text_iface is not None:
            try:
                length = int(text_iface.get_character_count())
                text_iface.set_text_contents(text) if hasattr(text_iface, "set_text_contents") else None
                if hasattr(text_iface, "delete_text") and hasattr(text_iface, "insert_text"):
                    text_iface.delete_text(0, length)
                    text_iface.insert_text(0, text, len(text))
                return
            except Exception:
                pass
        if _do_action(node, ("edit",)):
            return
        raise RuntimeError(f"cannot type into ref {ref}")

    def press_key(self, key: str) -> None:
        # Prefer the focused control; fall back to the active window action set.
        for node in list(self._refs.values()):
            states = _states(node)
            if "focused" in states and _do_action(node, (key.lower(), "press", "click")):
                return
        raise RuntimeError(f"cannot press {key}")

    def focus_window(self, index: int) -> None:
        position = int(index) - 1
        if position < 0 or position >= len(self._windows):
            raise RuntimeError(f"no window {index}")
        self._active = position
        node = self._windows[position]
        try:
            component = node.get_component_iface()
            if component is not None:
                component.grab_focus()
        except Exception as exc:
            raise RuntimeError(f"cannot focus window {index}") from exc

    def _ref(self, ref: str) -> Any:
        node = self._refs.get(str(ref).strip())
        if node is None:
            raise RuntimeError(f"unknown ref {ref}")
        return node

    def _load(self) -> tuple[Any, Any]:
        desktop = self._desktop()
        if desktop is None:
            raise RuntimeError("desktop accessibility is not available")
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        return Atspi, desktop

    def _desktop(self) -> Any | None:
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            return Atspi.get_desktop(0)
        except Exception:
            return None

    def _pick_window(self, desktop: Any, app: str) -> Any | None:
        windows = _application_windows(desktop)
        self._windows = windows
        if not windows:
            return None
        needle = app.strip().casefold()
        if needle:
            for index, window in enumerate(windows):
                label = f"{_node_name(window)} {_window_title(window)}".casefold()
                if needle in label:
                    self._active = index
                    return window
        if 0 <= self._active < len(windows):
            return windows[self._active]
        self._active = 0
        return windows[0]

    def _list_windows(self, desktop: Any, active: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, window in enumerate(self._windows or _application_windows(desktop), start=1):
            name = _node_name(window) or "app"
            title = _window_title(window) or name
            rows.append(
                {
                    "index": index,
                    "url": f"desktop://{quote(name, safe='')}",
                    "title": title,
                    "active": window is active,
                }
            )
        return rows


def _application_windows(desktop: Any) -> list[Any]:
    windows: list[Any] = []
    try:
        count = int(desktop.get_child_count())
    except Exception:
        return windows
    for index in range(count):
        try:
            app = desktop.get_child_at_index(index)
        except Exception:
            continue
        if app is None:
            continue
        try:
            kids = int(app.get_child_count())
        except Exception:
            kids = 0
        if kids <= 0:
            role = _role_name(app)
            if role in {"application", "frame", "window"}:
                windows.append(app)
            continue
        for child_index in range(kids):
            try:
                child = app.get_child_at_index(child_index)
            except Exception:
                continue
            if child is None:
                continue
            role = _role_name(child)
            if role in {"frame", "window", "dialog", "alert", "file chooser"}:
                windows.append(child)
            elif _role_name(app) == "application" and child_index == 0 and not windows:
                windows.append(child)
    return windows


def _walk(root: Any, atspi: Any) -> tuple[list[dict[str, Any]], str]:
    interactive: list[dict[str, Any]] = []
    content_lines: list[str] = []
    seen: set[int] = set()
    nodes = 0

    def visit(node: Any, depth: int, region: str) -> None:
        nonlocal nodes
        if node is None or depth > _MAX_DEPTH or nodes >= _MAX_NODES:
            return
        try:
            identity = id(node)
        except Exception:
            return
        if identity in seen:
            return
        seen.add(identity)
        nodes += 1
        role = _role_name(node)
        name = _node_name(node)
        next_region = _region(role, region)
        states = _states(node)
        if "showing" in states or "visible" in states or not states:
            if role in _INTERACTIVE and len(interactive) < _MAX_INTERACTIVE:
                value = _value(node)
                label = name or _neighbor_label(node) or "(unnamed)"
                interactive.append(
                    {
                        "ref": str(len(interactive) + 1),
                        "role": _short_role(role),
                        "name": label,
                        "region": next_region,
                        "states": [state for state in states if state in _STATE_KEEP],
                        "value": value,
                        "_node": node,
                    }
                )
            elif name and role in _CONTENT_ROLES and len(content_lines) < 80:
                line = " ".join(name.split())
                if len(line) >= 2:
                    content_lines.append(line)
        try:
            count = int(node.get_child_count())
        except Exception:
            return
        for index in range(count):
            try:
                child = node.get_child_at_index(index)
            except Exception:
                continue
            visit(child, depth + 1, next_region)

    visit(root, 0, "window")
    return interactive, "\n".join(content_lines)


_INTERACTIVE = {
    "push button",
    "button",
    "check box",
    "radio button",
    "combo box",
    "text",
    "entry",
    "password text",
    "menu item",
    "check menu item",
    "radio menu item",
    "link",
    "page tab",
    "tab",
    "slider",
    "spin button",
    "toggle button",
    "list item",
    "tree item",
    "scroll bar",
    "menu",
    "menu bar",
    "tool bar",
    "switch",
    "search box",
    "date editor",
}

_CONTENT_ROLES = {
    "label",
    "heading",
    "static",
    "text",
    "paragraph",
    "document frame",
    "document text",
    "status bar",
    "column header",
    "row header",
    "table cell",
}

_STATE_KEEP = {"focused", "checked", "pressed", "expanded", "selected", "required", "invalid", "busy", "modal"}


def _role_name(node: Any) -> str:
    try:
        return str(node.get_role_name() or "").strip().casefold()
    except Exception:
        return ""


def _node_name(node: Any) -> str:
    try:
        return str(node.get_name() or "").strip()
    except Exception:
        return ""


def _window_title(node: Any) -> str:
    name = _node_name(node)
    if name:
        return name
    try:
        count = int(node.get_child_count())
    except Exception:
        return ""
    for index in range(min(count, 8)):
        try:
            child = node.get_child_at_index(index)
        except Exception:
            continue
        title = _node_name(child)
        if title:
            return title
    return ""


def _region(role: str, current: str) -> str:
    if role in {"dialog", "alert", "file chooser"}:
        return "dialog"
    if role in {"menu", "menu bar", "menu item"}:
        return "menu"
    if role in {"tool bar"}:
        return "toolbar"
    if role in {"page tab list"}:
        return "tablist"
    return current


def _short_role(role: str) -> str:
    mapping = {
        "push button": "button",
        "check box": "checkbox",
        "radio button": "radio",
        "combo box": "combobox",
        "password text": "textbox",
        "text": "textbox",
        "entry": "textbox",
        "page tab": "tab",
        "menu item": "menuitem",
        "check menu item": "menuitem",
        "radio menu item": "menuitem",
        "spin button": "spinbutton",
        "toggle button": "button",
        "list item": "option",
        "tree item": "treeitem",
        "scroll bar": "scrollbar",
        "search box": "searchbox",
    }
    return mapping.get(role, role.replace(" ", "_") or "widget")


def _states(node: Any) -> list[str]:
    try:
        state_set = node.get_state_set()
    except Exception:
        return []
    names: list[str] = []
    for label in (
        "ACTIVE",
        "CHECKED",
        "COLLAPSED",
        "EDITABLE",
        "ENABLED",
        "EXPANDABLE",
        "EXPANDED",
        "FOCUSABLE",
        "FOCUSED",
        "MODAL",
        "PRESSED",
        "SELECTED",
        "SHOWING",
        "VISIBLE",
        "REQUIRED",
        "INVALID",
        "BUSY",
    ):
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            flag = getattr(Atspi.StateType, label, None)
            if flag is not None and state_set.contains(flag):
                names.append(label.casefold())
        except Exception:
            continue
    return names


def _value(node: Any) -> str:
    try:
        text = node.get_text_iface()
        if text is not None:
            return str(text.get_text(0, min(80, int(text.get_character_count()))) or "").strip()
    except Exception:
        pass
    try:
        value = node.get_value_iface()
        if value is not None:
            return str(value.get_current_value())
    except Exception:
        pass
    return ""


def _neighbor_label(node: Any) -> str:
    try:
        parent = node.get_parent()
    except Exception:
        return ""
    if parent is None:
        return ""
    try:
        count = int(parent.get_child_count())
        index = int(node.get_index_in_parent())
    except Exception:
        return ""
    for offset in (-1, 1, -2, 2):
        other = index + offset
        if other < 0 or other >= count:
            continue
        try:
            sibling = parent.get_child_at_index(other)
        except Exception:
            continue
        name = _node_name(sibling)
        if name:
            return f"near {name}"
    return ""


def _do_action(node: Any, names: tuple[str, ...]) -> bool:
    try:
        action = node.get_action_iface()
    except Exception:
        action = None
    if action is None:
        return False
    try:
        count = int(action.get_n_actions())
    except Exception:
        return False
    wanted = {name.casefold() for name in names}
    for index in range(count):
        try:
            label = str(action.get_name(index) or "").casefold()
        except Exception:
            continue
        if label in wanted or any(piece in label for piece in wanted):
            try:
                return bool(action.do_action(index))
            except Exception:
                return False
    if count > 0 and "click" in wanted:
        try:
            return bool(action.do_action(0))
        except Exception:
            return False
    return False


def _wants_desktop(task: str) -> bool:
    if _wants_page(task):
        return False
    return _DESKTOP_WORDS.search(task) is not None


def _app_from_task(task: str) -> str:
    match = re.search(
        r"\b(?:open|in|on|use)\s+(?:the\s+)?([A-Za-z][\w-]{1,40})(?:\s+(?:app|application|window))?\b",
        task,
        re.IGNORECASE,
    )
    if match:
        word = match.group(1).strip(" .,")
        if word.casefold() not in {"app", "desktop", "window", "application"}:
            return word
    return ""


def open_atspi() -> Surface:
    return AtspiSurface()


class Desktop(Capability):
    id = "desktop"
    tools = [
        Tool(
            name="read_screen",
            description="Read the structured text snapshot of the focused desktop window (URL, interactive refs, content).",
            parameters={"type": "object", "properties": {"app": {"type": "string"}}},
            effect=Effect.READ,
        ),
        Tool(
            name="click",
            description="Click an interactive desktop ref from the snapshot (for example 1) or a visible name.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="type_text",
            description="Type into a desktop textbox ref that is not a password.",
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}, "text": {"type": "string"}},
                "required": ["target", "text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="press_key",
            description="Press a key such as Enter, Tab, Escape, or ArrowDown in the focused desktop window.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="switch_page",
            description="Focus another desktop window by its Pages index from the snapshot.",
            parameters={
                "type": "object",
                "properties": {"index": {"type": "integer"}},
                "required": ["index"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="submit",
            description="Send or confirm from the desktop window. Waits for confirmation.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
            effect=Effect.EXTERNAL,
        ),
        Tool(
            name="pay",
            description="Pay or purchase from the desktop window. Waits for confirmation.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
            effect=Effect.EXTERNAL,
        ),
        Tool(
            name="delete_item",
            description="Delete or remove something in the desktop window. Waits for confirmation.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [
        FieldSpec("text", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, surface: Surface | None = None, *, open_surface: Any = None) -> None:
        self._surface = surface
        self._open = open_surface or open_atspi
        self._active = False
        self._last = ""

    def offered_tools(self, account_id: str, task: str) -> list[Tool]:
        if _wants_page(task):
            return []
        if self._active or _wants_desktop(task):
            return list(self.tools)
        return []

    def prepare(self, account_id: str, task: str) -> str:
        if not _wants_desktop(task):
            return ""
        try:
            text = self.invoke(account_id, "read_screen", {"app": _app_from_task(task)})
        except Exception as exc:
            return str(exc)
        if len(text) > 4000:
            text = text[:4000]
        return text

    def records(self, account_id: str) -> list[dict[str, str]]:
        if not self._last:
            return []
        text = self._last
        if len(text) > 800:
            text = text[:800]
        return [{"text": text}]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        surface = self._ensure()
        if tool_name == "read_screen":
            data = surface.snapshot(str(arguments.get("app", "")))
            text = _format_snapshot(data)
            self._active = True
            self._last = text
            return text
        before = self._last
        if tool_name == "click":
            target = str(arguments.get("target", ""))
            if _SENSITIVE.search(target):
                return "that action needs submit, pay, or delete_item so the person can confirm"
            surface.click(self._resolve(surface, target))
            return self._after(surface, before, f"clicked {target}")
        if tool_name == "type_text":
            target = str(arguments.get("target", ""))
            text = str(arguments.get("text", ""))
            surface.type_text(self._resolve(surface, target), text)
            return self._after(surface, before, f"typed into {target}")
        if tool_name == "press_key":
            key = str(arguments.get("key", ""))
            surface.press_key(key)
            return self._after(surface, before, f"pressed {key}")
        if tool_name == "switch_page":
            index = int(arguments.get("index", 0))
            surface.focus_window(index)
            return self._after(surface, before, f"focused window {index}")
        if tool_name in {"submit", "pay", "delete_item"}:
            target = str(arguments.get("target", "")).strip()
            if not target:
                data = surface.snapshot()
                target = _default_sensitive_target(data, tool_name)
            if not target:
                raise RuntimeError(f"no target for {tool_name}")
            surface.click(self._resolve(surface, target))
            return self._after(surface, before, f"{tool_name} {target}")
        raise NotImplementedError(tool_name)

    def _ensure(self) -> Surface:
        if self._surface is None:
            self._surface = self._open()
        if not self._surface.available():
            raise RuntimeError("desktop accessibility is not available")
        return self._surface

    def _resolve(self, surface: Surface, target: str) -> str:
        key = target.strip()
        if key.isdigit():
            return key
        data = surface.snapshot()
        for item in data.get("interactive") or ():
            name = str(item.get("name") or "")
            if name.casefold() == key.casefold() or key.casefold() in name.casefold():
                return str(item.get("ref") or "")
        raise RuntimeError(f"unknown target {target}")

    def _after(self, surface: Surface, before: str, lead: str) -> str:
        data = surface.snapshot()
        text = _format_snapshot(data)
        self._active = True
        self._last = text
        from robin.capabilities.browser import _action_diff

        diff = _action_diff(before, text)
        if diff:
            return f"{lead}; {diff}\n{text}"
        return f"{lead}\n{text}"


def _default_sensitive_target(data: dict[str, Any], tool_name: str) -> str:
    needles = {
        "submit": ("send", "submit", "confirm", "ok", "share"),
        "pay": ("pay", "purchase", "buy", "checkout"),
        "delete_item": ("delete", "remove", "trash"),
    }.get(tool_name, ())
    for item in data.get("interactive") or ():
        name = str(item.get("name") or "").casefold()
        if any(needle in name for needle in needles):
            return str(item.get("ref") or "")
    return ""
