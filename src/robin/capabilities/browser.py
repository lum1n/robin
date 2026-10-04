"""Browser on this instance. The model sees accessible text from the page."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from robin.capability import (
    ActiveTurn,
    Capability,
    Effect,
    FieldClass,
    FieldSpec,
    InputField,
    InputRequest,
    SecretAccepted,
    Tool,
    current_task,
)
from robin.vault import REFERENCE


def _active_text() -> str:
    turn = current_task.get()
    return turn.text if isinstance(turn, ActiveTurn) else ""



class Page(Protocol):
    def read(self, *, query: str = "", cursor: int = 0, region: str = "") -> tuple[str, str]: ...

    def set_checked(self, target: str, checked: bool, ref: str = "") -> None: ...

    def open(self, url: str) -> None: ...

    def click(self, target: str) -> None: ...

    def type_text(self, target: str, text: str) -> None: ...

    def type_password(self, text: str) -> None: ...

    def submit(self) -> None: ...

    def needs_login(self) -> bool: ...

    def type_username(self, text: str) -> None: ...

    def location(self) -> str: ...

    def sign_in(self, user: str, password: str) -> None: ...

    def needs_code(self) -> bool: ...

    def submit_code(self, code: str) -> None: ...


class PlaywrightPage:
    """Reads a structured accessible-text snapshot from a Playwright page."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._tabs: list[Any] = [page]
        self._active = 0
        self._refs: dict[str, tuple[str, str]] = {}
        self._next_ref = 1
        self._downloads: list[str] = []
        self._context: Any = None
        self._watch(page)

    def bind_context(self, context: Any) -> None:
        """Listen for popups and extra pages on this browser context."""
        self._context = context
        on = getattr(context, "on", None)
        if callable(on):
            on("page", self._adopt)
        for page in list(getattr(context, "pages", ()) or ()):
            if page in self._tabs:
                continue
            self._tabs.append(page)
            self._watch(page)

    def _watch(self, page: Any) -> None:
        on = getattr(page, "on", None)
        if not callable(on):
            return
        on("close", lambda: self._closed(page))
        on("download", lambda download: self._download(download))

    def _adopt(self, page: Any) -> None:
        if page in self._tabs:
            return
        self._tabs.append(page)
        self._active = len(self._tabs) - 1
        self._page = page
        self._watch(page)

    def _closed(self, page: Any) -> None:
        if page not in self._tabs:
            return
        index = self._tabs.index(page)
        del self._tabs[index]
        if not self._tabs:
            return
        if index < self._active:
            self._active -= 1
        elif index == self._active:
            self._active = min(self._active, len(self._tabs) - 1)
        self._page = self._tabs[self._active]

    def _download(self, download: Any) -> None:
        """Record a download. save_as must stay on the Playwright thread; the event already is."""
        name = ""
        try:
            name = str(getattr(download, "suggested_filename", "") or "")
        except Exception:
            name = ""
        if not name:
            try:
                name = str(download.url).rsplit("/", 1)[-1]
            except Exception:
                name = "download"
        profile = getattr(self, "_profile", None)
        if profile and name:
            folder = Path(profile) / "downloads"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / Path(name).name
                save = getattr(download, "save_as", None)
                if callable(save):
                    save(str(path))
                    name = str(path)
            except Exception:
                # Never raise from a Playwright event callback; it corrupts the sync greenlet.
                pass
        if name and name not in self._downloads:
            self._downloads.append(name)

    def page_list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, page in enumerate(self._tabs, start=1):
            try:
                url = str(getattr(page, "url", "") or "")
            except Exception:
                url = ""
            title = ""
            title_fn = getattr(page, "title", None)
            if callable(title_fn):
                try:
                    title = str(title_fn() or "")
                except Exception:
                    title = ""
            rows.append(
                {
                    "index": index,
                    "url": url,
                    "title": title,
                    "active": index == self._active + 1,
                }
            )
        return rows

    def downloads(self) -> list[str]:
        return list(self._downloads)

    def switch_page(self, index: int) -> None:
        position = int(index) - 1
        if position < 0 or position >= len(self._tabs):
            raise RuntimeError(f"no page {index}")
        self._active = position
        self._page = self._tabs[position]

    def open(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=25000)
        self.clear_gate()
        self.settle()

    def read(self, *, query: str = "", cursor: int = 0, region: str = "") -> tuple[str, str]:
        data = self._collect(query=query, region=region)
        controls = list(data.get("interactive") or [])
        listings = list(data.get("listings") or [])
        if query:
            needle = query.casefold()
            matched = [
                item for item in controls
                if needle in f"{item.get('name', '')} {item.get('group', '')} {item.get('role', '')}".casefold()
            ]
            listing_hits = [
                row for row in listings
                if needle in str(row.get("text") or "").casefold()
            ]
            listing_refs = {str(row.get("ref") or "") for row in listing_hits if row.get("ref")}
            seen = {str(item.get("ref") or "") for item in matched}
            for item in controls:
                ref = str(item.get("ref") or "")
                if ref in listing_refs and ref not in seen:
                    matched.append(item)
                    seen.add(ref)
            controls = matched
            if listing_hits:
                data["content"] = "\n".join(
                    str(row.get("text") or "") for row in listing_hits if row.get("text")
                )
        if region:
            controls = [item for item in controls if item.get("region") == region]
        data["total_controls"] = len(controls)
        data["cursor"] = cursor
        data["interactive"] = _window_interactive(controls)[cursor:cursor + _MAX_INTERACTIVE]
        data["pages"] = self.page_list()
        data["downloads"] = self.downloads()
        text = _format_snapshot(data)
        self._refs = _parse_refs(text)
        secrets = list(data.get("secrets") or [])
        for secret in secrets:
            if secret:
                text = text.replace(secret, "")
        return text, " ".join(secret for secret in secrets if secret)

    def _collect(self, *, query: str = "", region: str = "") -> dict[str, Any]:
        evaluate = getattr(self._page, "evaluate", None)
        if callable(evaluate):
            try:
                script = _SNAPSHOT_JS.replace("let nextRef = 1;", f"let nextRef = {self._next_ref};")
                script = script.replace("const discovery = {};", "const discovery = " + json.dumps(
                    {"query": query, "region": region}
                ) + ";")
                raw = evaluate(script)
                if isinstance(raw, dict):
                    controls = list(raw.get("interactive") or [])
                    for item in controls:
                        ref = str(item.get("ref") or "")
                        if ref.isdigit():
                            self._next_ref = max(self._next_ref, int(ref) + 1)
                    return {
                        "url": str(raw.get("url") or self.location()),
                        "title": str(raw.get("title") or ""),
                        "interactive": controls,
                        "content": str(raw.get("content") or ""),
                        "more_below": bool(raw.get("moreBelow") or raw.get("more_below")),
                        "secrets": list(raw.get("secrets") or []),
                        "discovery_limited": bool(raw.get("discoveryLimited")),
                        "scroll": raw.get("scroll"),
                        "listings": list(raw.get("listings") or []),
                        "result_count": str(raw.get("resultCount") or raw.get("result_count") or ""),
                        "dismissible_dialogs": list(
                            raw.get("dismissibleDialogs") or raw.get("dismissible_dialogs") or []
                        ),
                        "still_updating": bool(getattr(self, "_still_updating", False)),
                    }
            except Exception as exc:
                raise RuntimeError("could not inspect page controls — browser_read before continuing") from exc
        content = self._main_text()
        secrets = self._password_values()
        for secret in secrets:
            content = content.replace(secret, "")
        return {
            "url": self.location(),
            "title": "",
            "interactive": [],
            "content": content,
            "secrets": secrets,
            "listings": [],
            "result_count": "",
        }

    def _main_text(self) -> str:
        for selector in ("main", "article", "[role='main']"):
            found = self._page.locator(selector)
            try:
                if int(found.count()) == 0:
                    continue
                text = str(found.first.inner_text()).strip()
            except Exception:
                continue
            if len(text) >= 80:
                return text
        return str(self._page.locator("body").inner_text())

    def _password_values(self) -> list[str]:
        fields = self._fields("input[type='password']")
        values = [str(fields.nth(index).input_value()) for index in range(int(fields.count()))]
        codes = self._page.locator(_CODE_SELECTOR)
        for index in range(int(codes.count())):
            value = str(codes.nth(index).input_value())
            if value:
                values.append(value)
        return [value for value in values if value]

    def settle(self, *, timeout_ms: int = 4000) -> dict[str, Any]:
        """Wait until a SPA route finishes painting, not just a fixed pause."""
        # Cookie/consent overlays often load after first paint and block clicks/content.
        try:
            self.clear_gate()
        except Exception:
            pass
        page = self._page
        wait = getattr(page, "wait_for_load_state", None)
        if callable(wait):
            try:
                wait("domcontentloaded", timeout=min(5000, timeout_ms))
            except Exception:
                pass
        deadline = time.monotonic() + timeout_ms / 1000
        last = ""
        stable = 0
        self._still_updating = False
        while time.monotonic() < deadline:
            try:
                if int(self._fields("input[type='password']").count()) > 0:
                    self._still_updating = False
                    return {"still_updating": False}
            except Exception:
                pass
            try:
                url = str(getattr(page, "url", "") or "")
                inputs = int(self._fields("input, textarea, select").count())
                state = self._collect()
                fingerprint = _stable_fingerprint(url, inputs, state)
                loading = page.evaluate(
                    "() => !!document.querySelector('[aria-busy=\"true\"]')"
                ) is True if callable(getattr(page, "evaluate", None)) else False
            except Exception as exc:
                raise RuntimeError("could not verify page readiness — browser_read before continuing") from exc
            # A /login URL with no fields yet is still loading the SPA form.
            waiting_for_form = inputs == 0 and bool(_LOGIN_PATH.search(url))
            if fingerprint and fingerprint == last:
                stable += 1
                if stable >= 5 and not waiting_for_form and not loading:
                    self._still_updating = False
                    return {"still_updating": False}
            else:
                stable = 0
                last = fingerprint
            pause = getattr(page, "wait_for_timeout", None)
            if callable(pause):
                try:
                    pause(200)
                    continue
                except Exception:
                    pass
            time.sleep(0.2)
        self._still_updating = True
        return {"still_updating": True}

    def click(self, target: str, role: str = "", ref: str = "") -> None:
        if ref:
            overlay = self._click_ref(ref)
            if overlay is None:
                return
            try:
                self.clear_gate()
            except Exception:
                pass
            overlay = self._click_ref(ref)
            if overlay is None:
                return
            raise RuntimeError(overlay)
        if role in {"link", "button"}:
            try:
                control = self._page.get_by_role(role, name=target, exact=True)
                count = int(control.count())
                if count > 1:
                    raise RuntimeError(_ambiguous_message(target, count, role))
                if count > 0:
                    _raise_if_disabled(control.first, target)
                    _call_timeout(control.first.click, 5000)
                    return
            except RuntimeError:
                raise
            except Exception:
                pass
        try:
            control = self._page.get_by_text(target, exact=True)
            count = int(control.count())
            if count > 1 and not ref:
                raise RuntimeError(_ambiguous_message(target, count, "control"))
            if count > 0:
                _raise_if_disabled(control.first, target)
                _call_timeout(control.first.click, 5000)
                return
        except RuntimeError:
            raise
        except Exception:
            pass
        # Prefer a shorter distinctive fragment for long autocomplete / search labels.
        fragment = _click_fragment(target)
        if fragment and fragment != target:
            try:
                control = self._page.get_by_text(fragment, exact=False)
                count = int(control.count())
                if count == 1:
                    _raise_if_disabled(control.first, target)
                    _call_timeout(control.first.click, 5000)
                    return
                if count > 1:
                    raise RuntimeError(_ambiguous_message(target, count, "control"))
            except RuntimeError:
                raise
            except Exception:
                pass
        try:
            control = self._page.get_by_text(target)
            count = int(control.count())
            if count > 1:
                raise RuntimeError(_ambiguous_message(target, count, "control"))
            if count == 1:
                _raise_if_disabled(control.first, target)
                _call_timeout(control.first.click, 5000)
                return
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError(f'no clickable control matching "{target}"')

    def type_text(self, target: str, text: str, role: str = "", ref: str = "") -> None:
        if ref:
            if self._act_ref(ref, "fill", text):
                return
            raise RuntimeError(f"control [{ref}] is stale — browser_read for a fresh snapshot")
        try:
            labeled = self._page.get_by_label(target, exact=True)
            count = int(labeled.count())
            if count > 1:
                raise RuntimeError(_ambiguous_message(target, count, "field"))
            if count == 1:
                _call_timeout(labeled.first.fill, 5000, text)
                return
        except RuntimeError:
            raise
        except Exception:
            pass
        try:
            box = self._page.get_by_role(role or "textbox", name=target, exact=True)
            count = int(box.count())
            if count > 1:
                raise RuntimeError(_ambiguous_message(target, count, "field"))
            if count == 1:
                _call_timeout(box.first.fill, 5000, text)
                return
        except RuntimeError:
            raise
        except Exception:
            pass
        try:
            box = self._page.get_by_role("textbox", name=target, exact=True)
            count = int(box.count())
            if count > 1:
                raise RuntimeError(_ambiguous_message(target, count, "field"))
            if count == 1:
                _call_timeout(box.first.fill, 5000, text)
                return
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError(f'no text field matching "{target}"')

    def type_password(self, text: str) -> None:
        self._await_selector("input[type='password']", timeout_ms=8000)
        fields = self._fields("input[type='password']")
        if int(fields.count()) == 0:
            raise RuntimeError("no password field on this page")
        fields.first.fill(text)

    def select_option(self, target: str, value: str, ref: str = "") -> None:
        if ref:
            if self._act_ref(ref, "select", value):
                return
            raise RuntimeError(f"control [{ref}] is stale — browser_read for a fresh snapshot")
        labeled = self._page.get_by_label(target, exact=True)
        if int(labeled.count()) != 1:
            raise RuntimeError(f'no unique select matching "{target}" — use a current ref')
        self._select_control(labeled.first, value)

    def _select_control(self, control: Any, value: str, root: Any = None) -> None:
        tag = control.evaluate("(node) => node.tagName.toLowerCase()")
        if tag == "select":
            options = control.locator("option")
            labels = [str(options.nth(i).inner_text()).strip() for i in range(int(options.count()))]
            values = [str(options.nth(i).get_attribute("value") or "") for i in range(int(options.count()))]
            if labels.count(value) == 1:
                control.select_option(label=value, timeout=5000)
            elif values.count(value) == 1:
                control.select_option(value=value, timeout=5000)
            else:
                raise RuntimeError("option is missing or ambiguous — inspect this control's options")
            return
        if control.get_attribute("role") not in {"combobox", "listbox"}:
            raise RuntimeError("control is not a supported dropdown")
        _call_timeout(control.click, 5000)
        owner = control.get_attribute("aria-controls") or control.get_attribute("aria-owns")
        root = root or self._page
        if owner:
            root = root.locator(f"[id={json.dumps(owner)}]")
        option = root.get_by_role("option", name=value, exact=True)
        option.first.wait_for(state="visible", timeout=5000)
        if int(option.count()) != 1:
            raise RuntimeError("option is ambiguous — inspect the open listbox")
        _call_timeout(option.first.click, 5000)
        selected = control.evaluate(
            """(node, wanted) => node.value === wanted
              || node.getAttribute('aria-valuetext') === wanted
              || String(node.textContent || '').trim() === wanted""",
            value,
        )
        if not selected and (int(option.count()) != 1 or option.first.get_attribute("aria-selected") != "true"):
            raise RuntimeError("dropdown selection is not observable — browser_read to verify the selected option")

    def set_checked(self, target: str, checked: bool, ref: str = "") -> None:
        if not ref:
            raise RuntimeError("check-state changes require a current Interactive ref")
        if self._act_ref(ref, "check" if checked else "uncheck"):
            return
        raise RuntimeError(f"control [{ref}] is stale — browser_read for a fresh snapshot")

    def scroll(self, direction: str, ref: str = "") -> dict[str, Any]:
        amounts = {"up": -800, "down": 800, "top": -100000, "bottom": 100000}
        if direction.lower() not in amounts:
            raise ValueError("scroll direction must be up, down, top, or bottom")
        amount = amounts[direction.lower()]
        if ref:
            selector = f'[data-robin-ref="{ref}"]'
            try:
                for frame in self._action_frames():
                    locator = frame.locator(selector)
                    count = int(locator.count())
                    if count == 0:
                        continue
                    if count != 1:
                        raise RuntimeError("scroll container is ambiguous")
                    return locator.first.evaluate(
                        """(node, delta) => {
                          const refs = node.ownerDocument.defaultView.__robinControlRefs;
                          if (!refs || refs.get(node) !== node.getAttribute('data-robin-ref'))
                            throw new Error('stale scroll container');
                          if (node.scrollHeight <= node.clientHeight)
                            throw new Error('control is not a scroll container');
                          const before = node.scrollTop;
                          node.scrollBy({top: delta, behavior: 'instant'});
                          return {before, after: node.scrollTop, maximum: node.scrollHeight - node.clientHeight};
                        }""",
                        amount,
                    )
            except Exception as exc:
                raise RuntimeError("scroll container is stale or unavailable") from exc
            raise RuntimeError("scroll container is stale or unavailable")
        return self._page.evaluate("""delta => {
          const before = window.scrollY;
          window.scrollBy({top: delta, behavior: 'instant'});
          return {before, after: window.scrollY,
            maximum: Math.max(0, document.documentElement.scrollHeight - window.innerHeight)};
        }""", amount)

    def press_key(self, key: str) -> None:
        self._page.keyboard.press(key)

    def hover(self, target: str, role: str = "", ref: str = "") -> None:
        if ref:
            if self._act_ref(ref, "hover"):
                return
            raise RuntimeError(f"control [{ref}] is stale — browser_read for a fresh snapshot")
        if role:
            try:
                control = self._page.get_by_role(role, name=target, exact=True)
                count = int(control.count())
                if count > 1:
                    raise RuntimeError(_ambiguous_message(target, count, "control"))
                if count == 1:
                    control.first.hover(timeout=5000)
                    return
            except RuntimeError:
                raise
            except Exception:
                pass
        try:
            control = self._page.get_by_text(target, exact=True)
            count = int(control.count())
            if count > 1:
                raise RuntimeError(_ambiguous_message(target, count, "control"))
            if count == 1:
                control.first.hover(timeout=5000)
                return
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError(f'no hover target matching "{target}"')

    def type_focused(self, text: str) -> None:
        try:
            focused = self._page.locator(":focus")
            if int(focused.count()) > 0:
                focused.first.fill(text)
                return
        except Exception:
            pass
        self._page.keyboard.type(text)

    def go_back(self) -> None:
        self._page.go_back(wait_until="domcontentloaded", timeout=15000)

    def _action_frames(self) -> list[Any]:
        main = getattr(self._page, "main_frame", None)
        return [self._page] + [
            frame for frame in (getattr(self._page, "frames", None) or ())
            if frame is not None and frame is not main
        ]

    def _click_ref(self, ref: str) -> str | None:
        """Click a stamped ref. None means success; otherwise an overlay/stale message."""
        try:
            if self._act_ref(ref, "click"):
                return None
        except RuntimeError as exc:
            text = str(exc)
            if _is_overlay_result(text):
                return text
            raise
        return (
            f"control [{ref}] is gone or not clickable — browser_read for a fresh snapshot, "
            f"then click a current Interactive ref (do not retry [{ref}])"
        )

    def _act_ref(self, ref: str, action: str, text: str = "") -> bool:
        selector = f'[data-robin-ref="{ref}"]'
        saw = False
        last_error: Exception | None = None
        for frame in self._action_frames():
            try:
                locator = frame.locator(selector)
                count = int(locator.count())
                if count == 0:
                    continue
                if count != 1:
                    raise RuntimeError(f"control [{ref}] is ambiguous — browser_read")
                identity = locator.first.evaluate(
                    "(node) => { const refs = node.ownerDocument.defaultView.__robinControlRefs; "
                    "return !!refs && refs.get(node) === node.getAttribute('data-robin-ref'); }"
                ) if callable(getattr(locator.first, "evaluate", None)) else True
                if identity is False:
                    continue
                saw = True
                target = locator.first
                if action == "click":
                    try:
                        disabled = target.get_attribute("disabled")
                        aria = target.get_attribute("aria-disabled")
                    except Exception:
                        disabled, aria = None, None
                    if disabled is not None or aria == "true":
                        raise RuntimeError(
                            f'control [{ref}] is disabled — fill required fields first, then click again'
                        )
                    ok, detail = _click_locator(target)
                    if ok:
                        return True
                    if _is_pointer_intercept(detail):
                        cover = _overlay_name(target)
                        label = f'overlay "{cover}"' if cover else "an overlay"
                        raise RuntimeError(
                            f"control [{ref}] is covered by {label} that intercepts pointer "
                            f"— target [{ref}] still current"
                        )
                    last_error = RuntimeError(detail or "click failed")
                    continue
                if action == "select":
                    self._select_control(target, text, root=frame)
                elif action in {"check", "uncheck"}:
                    wanted = action == "check"
                    tag = target.evaluate("(node) => node.tagName.toLowerCase()")
                    if tag == "input" and target.get_attribute("type") in {"checkbox", "radio"}:
                        if target.is_checked() != wanted:
                            label_handle = target.evaluate_handle(
                                "(node) => node.labels?.length === 1 ? node.labels[0] : null"
                            )
                            try:
                                label = label_handle.as_element()
                                if label is not None and label.is_visible():
                                    _call_timeout(label.click, 5000)
                                else:
                                    target.set_checked(wanted, timeout=5000)
                            finally:
                                label_handle.dispose()
                        if target.is_checked() != wanted:
                            raise RuntimeError("requested check state was not applied")
                    elif target.get_attribute("role") in {"checkbox", "switch", "menuitemcheckbox"}:
                        current = target.get_attribute("aria-checked")
                        if current not in {"true", "false"}:
                            raise RuntimeError("check state is not observable")
                        if (current == "true") != wanted:
                            _call_timeout(target.click, 5000)
                        if target.get_attribute("aria-checked") != str(wanted).lower():
                            raise RuntimeError("requested check state was not applied")
                    else:
                        raise RuntimeError("control does not support check-state changes")
                elif action == "hover":
                    _call_timeout(target.hover, 5000)
                else:
                    _call_timeout(target.fill, 5000, text)
                    valid = target.evaluate(
                        "(node) => node.type !== 'number' || node.validity.valid"
                    ) if callable(getattr(target, "evaluate", None)) else True
                    if valid is False:
                        raise RuntimeError("numeric value is outside this field's allowed range")
                return True
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
                continue
        if saw:
            detail = str(last_error).splitlines()[0].strip() if last_error else ""
            suffix = f" ({detail[:120]})" if detail else ""
            verb = {"fill": "filled", "select": "selected", "hover": "hovered",
                    "check": "checked", "uncheck": "unchecked"}.get(action, "clicked")
            raise RuntimeError(
                f'control [{ref}] could not be {verb}{suffix} — browser_read, then try another Interactive ref'
            )
        return False

    def _fill_named(self, target: str, text: str) -> bool:
        needle = target.strip().lower()
        if not needle:
            return False
        selectors = (
            f'input[placeholder*="{target}" i], textarea[placeholder*="{target}" i]',
            f'input[name*="{target}" i], textarea[name*="{target}" i]',
            f'input[aria-label*="{target}" i], textarea[aria-label*="{target}" i]',
            f'input[id*="{target}" i], textarea[id*="{target}" i]',
        )
        for selector in selectors:
            try:
                found = self._fields(selector)
                if int(found.count()) == 0:
                    continue
                found.first.fill(text, timeout=5000)
                return True
            except Exception:
                continue
        # Case-insensitive placeholder contains check via role textboxes.
        try:
            boxes = self._page.get_by_role("textbox")
            total = int(boxes.count())
        except Exception:
            return False
        for index in range(min(total, 20)):
            try:
                box = boxes.nth(index)
                name = str(box.get_attribute("placeholder") or box.get_attribute("name") or box.get_attribute("aria-label") or "")
                if needle in name.lower():
                    box.fill(text, timeout=5000)
                    return True
            except Exception:
                continue
        return False

    def submit(self) -> None:
        if self._submit_in_password_form():
            return
        buttons = self._page.locator("button[type='submit'], input[type='submit']")
        chosen = _pick_submit_button(buttons)
        if chosen is not None:
            chosen.click()
            return
        if self._click_named_submit():
            return
        if self._click_submit_ref():
            return
        raise RuntimeError(
            "no type=submit control — if Interactive lists a send/book button, browser_click that ref"
        )

    def _click_named_submit(self) -> bool:
        """Click a visible booking/send button that is not type=submit."""
        for label in _FORM_SUBMIT_LABELS:
            try:
                control = self._page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.IGNORECASE))
                count = int(control.count())
                if count <= 0:
                    control = self._page.get_by_role("button", name=re.compile(label, re.IGNORECASE))
                    count = int(control.count())
                if count <= 0:
                    continue
                target = control.nth(count - 1)
                _raise_if_disabled(target, label)
                _call_timeout(target.click, 5000)
                return True
            except RuntimeError:
                raise
            except Exception:
                continue
        return False

    def _click_submit_ref(self) -> bool:
        """Use the latest Interactive snapshot to find a submit-like button."""
        best_ref = ""
        best_score = 0
        for ref, (role, name) in self._refs.items():
            if role != "button":
                continue
            score = _submit_score(name)
            if score > best_score:
                best_score = score
                best_ref = ref
        if not best_ref:
            return False
        return bool(self._act_ref(best_ref, "click"))

    def needs_login(self) -> bool:
        """True only when a visible password field is present (a real login form).

        Email/search text boxes plus a header "Log in" link must not count — flight
        OTAs and shops always have those and would trap every browse in a sign-in ask.
        """
        try:
            passwords = self._fields("input[type='password']")
            count = int(passwords.count())
        except Exception:
            return False
        if count <= 0:
            return False
        visible = 0
        checked = 0
        for index in range(count):
            try:
                node = passwords.nth(index)
                checked += 1
                if bool(node.is_visible()):
                    visible += 1
            except Exception:
                continue
        if checked == 0:
            return True
        return visible > 0

    def type_username(self, text: str) -> None:
        self._await_selector(_USER_SELECTOR, timeout_ms=8000)
        field = self._fields(_USER_SELECTOR)
        if int(field.count()) == 0:
            raise RuntimeError("no username field on this page")
        field.first.fill(text)

    def clear_gate(self) -> None:
        """Dismiss cookie/consent overlays, including Sourcepoint-style iframes."""
        labels = (
            "Godta alle",
            "Tillat alle",
            "Accept all",
            "Allow all",
            "Accept All",
            "I agree",
            "Agree",
            "Accept",
            "Godta",
            "Tillat",
            "Jeg forstår",
            "Forstått",
            "Bare nødvendige",
            "Kun nødvendige",
            "Avvis alle",
            "Avvis",
            "Avslå",
            "Nekt alle",
            "Lukk",
            "Fortsett uten",
            "Reject all",
            "Reject",
            "Close",
            "Got it",
            "I understand",
        )
        # CMP iframes often appear a beat after first paint.
        for _attempt in range(6):
            if self._click_gate_label(labels):
                return
            try:
                self._page.wait_for_timeout(300)
            except Exception:
                time.sleep(0.3)

    def _click_gate_label(self, labels: tuple[str, ...]) -> bool:
        frames: list[Any] = [self._page]
        frames.extend(frame for frame in (getattr(self._page, "frames", None) or ()) if frame is not None)
        for frame in frames:
            for label in labels:
                try:
                    control = frame.get_by_role("button", name=label)
                    count = int(control.count())
                    if count <= 0:
                        control = frame.get_by_role(
                            "button",
                            name=re.compile(rf"^{re.escape(label)}$", re.IGNORECASE),
                        )
                        count = int(control.count())
                    if count <= 0:
                        continue
                    _call_timeout(control.first.click, 2500)
                    try:
                        self._page.wait_for_timeout(400)
                    except Exception:
                        time.sleep(0.4)
                    return True
                except Exception:
                    continue
        return False

    def location(self) -> str:
        return str(getattr(self._page, "url", "") or "")

    def sign_in(self, user: str, password: str) -> None:
        self.clear_gate()
        self._await_selector(f"{_USER_SELECTOR}, input[type='password']", timeout_ms=10000)
        self.type_username(user)
        if int(self._fields("input[type='password']").count()) == 0:
            self._press(_LOGIN_CONTINUE)
            try:
                self._fields("input[type='password']").first.wait_for(timeout=8000)
            except Exception:
                pass
        self.type_password(password)
        self._press(_LOGIN_SUBMIT)
        self.settle()

    def _await_selector(self, selector: str, *, timeout_ms: int) -> None:
        """Wait for a SPA form control after navigation."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                if int(self._fields(selector).count()) > 0:
                    return
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return
            pause = getattr(self._page, "wait_for_timeout", None)
            if callable(pause):
                try:
                    pause(200)
                    continue
                except Exception:
                    pass
            time.sleep(0.2)

    def _fields(self, selector: str) -> Any:
        found = self._page.locator(selector)
        try:
            if int(found.count()) > 0:
                return found
        except Exception:
            return found
        for frame in getattr(self._page, "frames", ()) or ():
            if frame is getattr(self._page, "main_frame", None):
                continue
            try:
                nested = frame.locator(selector)
                if int(nested.count()) > 0:
                    return nested
            except Exception:
                continue
        return found

    def _press(self, labels: tuple[str, ...]) -> None:
        if self._press_in_password_form(labels):
            return
        for label in labels:
            try:
                control = self._page.get_by_role("button", name=label)
                count = int(control.count())
                if count > 0:
                    # Prefer the last match: header/nav often repeats the label first.
                    control.nth(count - 1).click()
                    return
            except Exception:
                pass
            try:
                control = self._page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.IGNORECASE))
                count = int(control.count())
                if count > 0:
                    control.nth(count - 1).click()
                    return
            except Exception:
                pass
            try:
                control = self._page.get_by_text(label, exact=True)
                count = int(control.count())
                if count > 0:
                    control.nth(count - 1).click()
                    return
            except Exception:
                pass
            try:
                control = self._page.get_by_text(label)
                if int(control.count()) > 0:
                    control.first.click()
                    return
            except Exception:
                continue
        self.submit()

    def _press_in_password_form(self, labels: tuple[str, ...]) -> bool:
        try:
            forms = self._page.locator("form:has(input[type='password'])")
            if int(forms.count()) == 0:
                return False
            form = forms.first
        except Exception:
            return False
        for label in labels:
            try:
                buttons = form.get_by_role("button", name=label)
                count = int(buttons.count())
                if count > 0:
                    buttons.nth(count - 1).click()
                    return True
            except Exception:
                continue
        try:
            submits = form.locator("button[type='submit'], input[type='submit']")
            chosen = _pick_submit_button(submits)
            if chosen is not None:
                chosen.click()
                return True
        except Exception:
            return False
        return False

    def _submit_in_password_form(self) -> bool:
        try:
            forms = self._page.locator("form:has(input[type='password'])")
            if int(forms.count()) == 0:
                return False
            submits = forms.first.locator("button[type='submit'], input[type='submit']")
            chosen = _pick_submit_button(submits)
            if chosen is not None:
                chosen.click()
                return True
        except Exception:
            return False
        return False

    def needs_code(self) -> bool:
        if int(self._page.locator(_CODE_SELECTOR).count()) > 0:
            return True
        text = str(self._page.locator("body").inner_text())
        if len(text) > 400 or not _TWO_FACTOR.search(text):
            return False
        return int(self._page.locator("input[name*='code' i], input[type='tel'], input[inputmode='numeric']").count()) > 0

    def submit_code(self, code: str) -> None:
        field = self._page.locator(_CODE_SELECTOR)
        if int(field.count()) == 0:
            field = self._page.locator("input[name*='code' i], input[type='tel'], input[inputmode='numeric']")
        field.first.fill(code)
        button = self._page.locator("button[type='submit'], input[type='submit']")
        if int(button.count()) > 0:
            button.first.click()
            return
        for label in ("Verify", "Continue", "Submit"):
            control = self._page.get_by_text(label)
            if int(control.count()) > 0:
                control.first.click()
                return
        self.submit()


def _checkout_browsers() -> Path:
    return Path(__file__).resolve().parents[3] / ".ms-playwright"


def _browser_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if configured and configured != "0":
        roots.append(Path(configured))
    roots.append(_checkout_browsers())
    roots.append(Path.home() / ".cache" / "ms-playwright")
    return roots


def _chromium_executable() -> Path | None:
    for root in _browser_roots():
        for path in sorted(root.glob("chromium-*/chrome-linux64/chrome")):
            if path.is_file() and os.access(path, os.X_OK):
                return path
        for path in sorted(root.glob("chromium-*/chrome-linux/chrome")):
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return None


_HANDOFF_PREFIX = "HANDOFF:"
_VIEWPORT = {"width": 1920, "height": 1080}
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-size=1920,1080",
]
_IGNORE_ARGS = ["--enable-automation"]


def launch_options(*, headless: bool | None = None, account_id: str = "") -> dict[str, Any]:
    """Options passed to Chromium launch / persistent context. Tests assert these."""
    from robin.capabilities.vdisplay import headless_requested

    use_headless = headless_requested() if headless is None else headless
    options: dict[str, Any] = {
        "headless": use_headless,
        "ignore_default_args": list(_IGNORE_ARGS),
        "args": list(_LAUNCH_ARGS),
        "accept_downloads": True,
        "locale": "nb-NO",
        "timezone_id": "Europe/Oslo",
        "viewport": dict(_VIEWPORT),
        "account_id": account_id,
    }
    if _chrome_available():
        options["channel"] = "chrome"
    return options


def _chrome_available() -> bool:
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        path = Path("/usr/bin") / name
        if path.is_file() and os.access(path, os.X_OK):
            return True
    return False


def _sync_api():
    from robin.capabilities.vdisplay import browser_engine

    engine = browser_engine()
    if engine == "patchright":
        from patchright.sync_api import sync_playwright

        return sync_playwright, "chromium"
    if engine == "camoufox":
        try:
            from camoufox.sync_api import Camoufox

            return Camoufox, "camoufox"
        except Exception:
            from playwright.sync_api import sync_playwright

            return sync_playwright, "chromium"
    from playwright.sync_api import sync_playwright

    return sync_playwright, "chromium"


def open_chromium(url: str, profile: Path | None = None, *, account_id: str = "") -> PlaywrightPage:
    from robin.capabilities.vdisplay import browser_engine, display_for, headless_requested

    options = launch_options(account_id=account_id)
    use_headless = bool(options["headless"])
    display = None
    if not use_headless and not os.environ.get("DISPLAY"):
        try:
            display = display_for(account_id or "_default")
            display.start()
            os.environ["DISPLAY"] = display.display
        except RuntimeError:
            # No Xvfb on this machine — fall back to headless rather than hard-fail.
            use_headless = True
            display = None
            options = {**options, "headless": True}

    engine = browser_engine()
    if engine == "camoufox":
        return _open_camoufox(url, profile=profile, options=options, display=display)

    sync_playwright, _kind = _sync_api()
    executable = None if options.get("channel") else _chromium_executable()
    if executable is None and not options.get("channel"):
        raise RuntimeError("Chromium is not installed. Run playwright install chromium on this machine.")
    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        launch_kwargs: dict[str, Any] = {
            "headless": use_headless,
            "ignore_default_args": list(options["ignore_default_args"]),
            "args": list(options["args"]),
        }
        if options.get("channel"):
            launch_kwargs["channel"] = options["channel"]
        elif executable is not None:
            launch_kwargs["executable_path"] = str(executable)

        context_kwargs: dict[str, Any] = {
            "accept_downloads": True,
            "locale": options["locale"],
            "timezone_id": options["timezone_id"],
            "viewport": dict(options["viewport"]),
        }
        if profile is not None:
            profile.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                **launch_kwargs,
                **context_kwargs,
            )
            page = context.pages[0] if context.pages else context.new_page()
            opened = PlaywrightPage(page)
            opened._profile = str(profile)
            opened.bind_context(context)
            opened._playwright = playwright
            opened._context = context
            opened._display = display
            opened._account_id = account_id
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            opened.clear_gate()
            return opened
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        opened = PlaywrightPage(page)
        opened.bind_context(context)
        opened._playwright = playwright
        opened._browser = browser
        opened._context = context
        opened._display = display
        opened._account_id = account_id
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        opened.clear_gate()
        return opened
    except Exception:
        # A failed goto must not leave sync Playwright's asyncio loop running on
        # this thread — the next open would raise "Sync API inside the asyncio loop".
        if display is not None:
            try:
                display.stop()
            except Exception:
                pass
        _abandon_playwright(playwright, browser=browser, context=context)
        raise


def _open_camoufox(
    url: str,
    *,
    profile: Path | None,
    options: dict[str, Any],
    display: Any,
) -> PlaywrightPage:
    from camoufox.sync_api import Camoufox

    kwargs: dict[str, Any] = {
        "headless": bool(options["headless"]),
        "humanize": True,
    }
    if profile is not None:
        profile.mkdir(parents=True, exist_ok=True)
        kwargs["persistent_context"] = str(profile)
    browser = None
    try:
        browser = Camoufox(**kwargs)
        browser.start()
        page = browser.new_page() if hasattr(browser, "new_page") else browser.pages[0]
        # Camoufox context manager style — treat as context.
        opened = PlaywrightPage(page)
        opened._browser = browser
        opened._display = display
        opened._profile = str(profile) if profile else ""
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        opened.clear_gate()
        return opened
    except Exception:
        if display is not None:
            try:
                display.stop()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        raise


def _abandon_playwright(playwright: Any, *, browser: Any = None, context: Any = None) -> None:
    for closer in (context, browser):
        if closer is None:
            continue
        try:
            closer.close()
        except Exception:
            pass
    try:
        playwright.stop()
    except Exception:
        pass


class _BrowserCalls:
    """Playwright is bound to the thread that started it. Every page call uses that thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        thread = threading.Thread(target=self._loop, name="robin-browser", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while True:
            function, done = self._queue.get()
            try:
                done["value"] = function()
            except Exception as exc:
                done["error"] = exc
            done["ready"].set()

    def run(self, function: Any) -> Any:
        done: dict[str, Any] = {"ready": threading.Event()}
        self._queue.put((function, done))
        done["ready"].wait()
        if "error" in done:
            raise done["error"]
        return done.get("value")


class Desk:
    """Pages for one account. Popups stay with that account's browser session."""

    def __init__(self, opener: Any, *, profiles: Path | None = None) -> None:
        self.opener = opener
        self.profiles = profiles
        self.pages: dict[str, Page] = {}
        self._calls = _BrowserCalls()
        self._lock = threading.Lock()

    def has(self, account_id: str) -> bool:
        with self._lock:
            return account_id in self.pages

    def open(self, account_id: str, url: str) -> Page:
        def work() -> Page:
            target = _web_url(url)
            with self._lock:
                current = self.pages.get(account_id)
            if current is None:
                current = self._launch(account_id, target)
                with self._lock:
                    self.pages[account_id] = current
                return current
            current.open(target)
            return current

        return self._calls.run(work)

    def _launch(self, account_id: str, url: str) -> Page:
        profile = None
        if self.profiles is not None and account_id:
            profile = (self.profiles / account_id / "browser").resolve()
        try:
            return self.opener(url, profile, account_id=account_id)
        except TypeError:
            if profile is None:
                return self.opener(url)
            try:
                return self.opener(url, profile)
            except TypeError:
                try:
                    return self.opener(url, profile=profile)
                except TypeError:
                    return self.opener(url)

    def run(self, account_id: str, function: Any) -> Any:
        def work() -> Any:
            with self._lock:
                page = self.pages.get(account_id)
            if page is None:
                raise RuntimeError("no page is open")
            return function(page)

        return self._calls.run(work)

    def screenshot(self, account_id: str) -> bytes:
        """PNG of the live page for the person's live view — never sent to a model."""

        def work(page: Page) -> bytes:
            raw = getattr(page, "_page", None)
            if raw is None:
                raise RuntimeError("no live page")
            shot = getattr(raw, "screenshot", None)
            if not callable(shot):
                raise RuntimeError("screenshot unavailable")
            return bytes(shot(type="png", full_page=False))

        return self.run(account_id, work)


_SITE = re.compile(r"(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
_EXPLICIT_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


_CREDENTIALS = re.compile(
    r"(?:user(?:name)?|e-?mail|login)\s*(?:is|:)?\s*(\S+)\s+(?:and\s+)?(?:my\s+)?(?:password|passcode)\s*(?:is|:)?\s*(\S.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL_ONLY = re.compile(r"^\s*(?:cancel|stop|never mind|nevermind)\s*\.?\s*$", re.IGNORECASE)
_CANCEL_LEAD = re.compile(r"^\s*(?:cancel|stop|never mind|nevermind)\b", re.IGNORECASE)


def _page_url(task: str) -> str | None:
    found = _EXPLICIT_URL.search(task)
    if found:
        return found.group(0).rstrip(".,);]")
    site = _SITE.search(task.lower())
    if site is None:
        return None
    return "https://" + site.group(0)


def _wants_page(task: str) -> bool:
    if _page_url(task) is not None:
        return True
    lowered = task.lower()
    if any(word in lowered for word in ("https://", "http://", "website", "web page", "webpage", "browser")):
        return True
    return _SITE.search(lowered) is not None


class Browser(Capability):
    id = "display"
    tools = [
        Tool(
            name="browser_open",
            description=(
                "Open an http or https URL and return a text snapshot with URL, Interactive refs, and Content. "
                "Use this for any website task: booking, shopping, reading, signing in, filling forms. "
                "Pass a full URL such as https://example.com (a bare host is accepted and treated as https). "
                "A site named without a domain (a store, brand, or service name, or its ORG reference) is still a site: "
                "pass that name and Robin finds its website. "
                "The site the latest Person line names always wins: if the open page is a different host, open the named one first. "
                "If this site is already open, prefer browser_click/read on the current page instead of opening the homepage again. "
                "Never pass a booking choice word (clinic, home visit, consultation) as the url — click that option on the page. "
                "When the person names a site with an ORG reference (or similar), pass that entire reference as url — confirm restores the real host. "
                "Do not invent a different hostname from memory (for example lot.com when they said Google). "
                "Reach the site's actual search/results page, not just its category landing page. "
                "Type only the product the person named into site search — do not invent a model year "
                "or extra filters they did not ask for. "
                "When the snapshot includes Listings or Price range, answer from those (prices and the range). "
                "To click or type, use Interactive refs (for example target 1) or the visible name. "
                "Use browser_find or scoped browser_read to discover omitted search/filter/sort controls before scrolling blindly. "
                "Use browser_select for dropdowns, browser_scroll to reveal more, browser_press for Enter or Tab, "
                "browser_back to leave a page. Use browser_hover for menus, browser_type_focused when the caret is already in a field. "
                "When a snapshot says a saved sign-in exists, use browser_fill_username and browser_fill_password; never invent the password. "
                "When a snapshot lists saved profile fields, use browser_fill_profile for those — never type email, phone, or name yourself. "
                "When Pages lists more than one entry, browser_switch focuses that popup by index. "
                "If Content ends with (more below), scroll only to read more Content — Interactive refs are already listed and clickable. "
                "Empty, truncated or withheld Content does not mean zero search results. "
                "Listings and Price range are the ads on the page — report those prices; do not say you found nothing. "
                "Only claim no matches when the page explicitly says so after the requested filters are applied. "
                "If a page snapshot says bot/captcha wall, stop hopping sites — "
                "tell the person automation was blocked and use web_search for a rough estimate or ask which site to try. "
                "An unread cross-origin frame is not a captcha — report the host and keep using the readable page. "
                "If conversation history already says a host blocked Robin's automated browser, do not open that "
                "same host again — say it is still blocked from this machine and offer web_search or another site."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            effect=Effect.MUTATE,
            egress=True,
        ),
        Tool(
            name="browser_read",
            description=(
                "Read the structured text snapshot of this account's page (URL, interactive refs, content). "
                "Use a query or region to find omitted search/filter/sort controls before scrolling blindly. "
                "Empty or truncated Content is not zero results. Listings and Price range are visible ads. "
                "(more below) only means Content text is truncated; "
                "do not scroll away from a form to find a button that is already listed under Interactive."
            ),
            parameters={"type": "object", "properties": {
                "query": {"type": "string"},
                "region": {"type": "string", "enum": ["dialog", "main", "nav", "header", "page"]},
                "cursor": {"type": "integer", "minimum": 0, "maximum": 2000},
            }},
            effect=Effect.READ,
        ),
        Tool(
            name="browser_find",
            description=(
                "Find omitted controls by label, group, role, or visible Content/listing text. "
                "Matching listing lines return the nearest Interactive ref. Use before blind scrolling. "
                "No observable change is not progress — try a different justified action after two unchanged attempts."
            ),
            parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            effect=Effect.READ,
        ),
        Tool(
            name="browser_set_checked",
            description=(
                "Set a checkbox or switch to checked=true or false, without toggling an already correct state. "
                "Use this for search filters the person asked for. Do not add a year or extra filter they did not name. "
                "Verify range limits and selected sorting in the new snapshot."
            ),
            parameters={"type": "object", "properties": {
                "target": {"type": "string"}, "checked": {"type": "boolean"},
            }, "required": ["target", "checked"]},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_click",
            description=(
                "Click an interactive ref from the snapshot (for example 1) or a visible name. "
                "Prefer a ref when the same name appears more than once. "
                "Use this to follow booking buttons, menus, and links on the open page. "
                "When the person describes a choice in plain language (clinic, home visit, consultation), "
                "match it to a control and click — do not ask them to pick 1/2/3, and never invent a numbered menu. "
                "Words like clinic or home visit are page choices — click them after the site is open; never browser_open them as a URL. "
                "If the snapshot marks a control disabled, fill required fields first. "
                "A button listed under Interactive is available — click its ref; do not claim it is missing. "
                "For named actions such as Send bestilling, click that ref; browser_submit is only for type=submit login forms. "
                "If a click fails, browser_read and try a different ref — do not retry the same target. "
                "After typing a search query, browser_press Enter or click a search suggestion option."
            ),
            parameters={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_type",
            description=(
                "Type into a textbox ref or labeled field that is not a password. "
                "For site search, type the product the person named — do not add a year they did not say. "
                "Prefer an Interactive ref number (for example 12). "
                "For email, phone, name, or address, prefer browser_fill_profile when a saved profile exists — "
                "never invent contact details and never type them here."
            ),
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}, "text": {"type": "string"}},
                "required": ["target", "text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_select",
            description="Choose an option in a combobox or select ref.",
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}, "value": {"type": "string"}},
                "required": ["target", "value"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_scroll",
            description="Scroll the page or a ref. Direction is up, down, top, or bottom.",
            parameters={
                "type": "object",
                "properties": {
                    "direction": {"type": "string"},
                    "target": {"type": "string"},
                },
                "required": ["direction"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_press",
            description="Press a key such as Enter, Tab, Escape, or ArrowDown.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_hover",
            description="Hover an interactive ref or visible name to reveal menus.",
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_type_focused",
            description="Type into the focused field when it has no useful label or ref.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_back",
            description="Go back one page in the browser history.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_switch",
            description="Focus another open page or popup by its Pages index from the snapshot.",
            parameters={
                "type": "object",
                "properties": {"index": {"type": "integer"}},
                "required": ["index"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_fill_username",
            description=(
                "Fill the saved username/email for this site into a textbox ref or label. "
                "Use when the page shows a sign-in form and a saved sign-in exists."
            ),
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_fill_password",
            description=(
                "Fill the saved password for this site into a password field or ref. "
                "Prefer a password ref from the snapshot, or call with target empty. "
                "Waits for confirmation. The password never appears in the tool arguments."
            ),
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
            effect=Effect.EXTERNAL,
        ),
        Tool(
            name="browser_fill_profile",
            description=(
                "Fill one saved personal detail into a textbox ref or label. "
                "field is given_name, family_name, full_name, email, phone, address, city, postal_code, or country. "
                "The value is loaded from this account's profile and never appears in the tool arguments. "
                "Use when the snapshot lists saved profile fields."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "target": {"type": "string"},
                },
                "required": ["field", "target"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="browser_type_password",
            description="Type a password the person just provided. Waits for confirmation.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            effect=Effect.EXTERNAL,
            drop_arguments=("text",),
        ),
        Tool(
            name="browser_submit",
            description=(
                "Submit a login-style form that has a type=submit control. "
                "For booking or named buttons such as Send bestilling, prefer browser_click on that Interactive ref."
            ),
            parameters={"type": "object", "properties": {}},
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [
        FieldSpec("text", FieldClass.ORDINARY, free_text=True),
        FieldSpec("password", FieldClass.DROP),
    ]

    def __init__(self, owner: str | None = None, page: Page | None = None, *, desk: Desk | None = None, broker: Any = None) -> None:
        if desk is None and (owner is None or page is None):
            raise ValueError("browser needs a page or a desk")
        self.owner = owner
        self.page = page
        self.desk = desk
        self.broker = broker
        self._direct: dict[str, str] = {}
        self._waits: dict[tuple[str, str], dict[str, Any]] = {}
        self._codes: dict[tuple[str, str], str] = {}
        self._once: dict[str, list[str]] = {}
        self._refs: dict[str, dict[str, tuple[str, str]]] = {}
        self._last_snapshots: dict[str, str] = {}
        self._login_notes: dict[str, str] = {}
        # (account_id, name) -> https URL of that site's homepage; set by install from web search.
        self.find_site: Callable[[str, str], str | None] | None = None

    def accept_secret(self, account_id: str, conversation_id: str, text: str) -> SecretAccepted | None:
        waiting = self._wait(account_id, conversation_id)
        if waiting is None:
            return None
        if _CANCEL_ONLY.match(text):
            self._codes.pop((account_id, conversation_id), None)
            self._clear_wait(account_id, conversation_id)
            return SecretAccepted(reply="Sign-in cancelled.")
        if _CANCEL_LEAD.match(text):
            # "never mind, what is 2+2?" abandons the prompt and continues as ordinary chat.
            self._codes.pop((account_id, conversation_id), None)
            self._clear_wait(account_id, conversation_id)
            return None
        if waiting.get("kind") == "code":
            code = _parse_code(text)
            if code is None:
                # A new question abandons the code prompt so the conversation is not trapped.
                self._clear_wait(account_id, conversation_id)
                return None
            self._codes[(account_id, conversation_id)] = code
            self._clear_wait(account_id, conversation_id)
            return SecretAccepted(
                resume=str(waiting["task"]),
                allow_cloud=bool(waiting.get("allow_cloud")),
                free_text=bool(waiting.get("free_text")),
            )
        parsed = _parse_credentials(text)
        if parsed is None:
            # Same escape: ordinary chat must not keep replaying the sign-in prompt.
            self._clear_wait(account_id, conversation_id)
            return None
        user, password = parsed
        self._save(account_id, list(waiting["hosts"]), user, password)
        self._clear_wait(account_id, conversation_id)
        return SecretAccepted(
            resume=str(waiting["task"]),
            allow_cloud=bool(waiting.get("allow_cloud")),
            free_text=bool(waiting.get("free_text")),
        )

    def pending_input(self, account_id: str, conversation_id: str) -> InputRequest | None:
        waiting = self._wait(account_id, conversation_id)
        if waiting is None:
            return None
        host = (waiting.get("hosts") or ["that site"])[0]
        request_id = str(waiting.get("request_id") or f"browser-{conversation_id}")
        failed = bool(waiting.get("failed"))
        if waiting.get("kind") == "code":
            return InputRequest(
                request_id=request_id,
                title=f"Code for {host}",
                reason=self._direct.get(account_id) or _ask_code(host, failed=failed),
                fields=(InputField(id="code", label="Code", kind="otp"),),
                owner=self.id,
            )
        return InputRequest(
            request_id=request_id,
            title=f"Sign in to {host}",
            reason=self._direct.get(account_id) or _ask_text(host, failed=failed),
            fields=(
                InputField(id="username", label="Username", kind="username"),
                InputField(id="password", label="Password", kind="secret"),
            ),
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
        waiting = self._wait(account_id, conversation_id)
        if waiting is None:
            return None
        expected = str(waiting.get("request_id") or f"browser-{conversation_id}")
        if request_id != expected:
            return None
        if cancel:
            self._codes.pop((account_id, conversation_id), None)
            self._clear_wait(account_id, conversation_id)
            return SecretAccepted(reply="Sign-in cancelled.")
        if waiting.get("kind") == "code":
            code = str(values.get("code") or "").strip()
            if not code:
                return SecretAccepted(reply="Enter the code.")
            self._codes[(account_id, conversation_id)] = code
            self._clear_wait(account_id, conversation_id)
            return SecretAccepted(
                resume=str(waiting["task"]),
                allow_cloud=bool(waiting.get("allow_cloud")),
                free_text=bool(waiting.get("free_text")),
            )
        user = str(values.get("username") or "").strip()
        password = str(values.get("password") or "").strip()
        if not user or not password:
            return SecretAccepted(reply="Username and password are required.")
        self._save(account_id, list(waiting["hosts"]), user, password)
        self._clear_wait(account_id, conversation_id)
        return SecretAccepted(
            resume=str(waiting["task"]),
            allow_cloud=bool(waiting.get("allow_cloud")),
            free_text=bool(waiting.get("free_text")),
        )

    def peel_secret(self, account_id: str, text: str) -> str | None:
        if self.broker is None:
            return None
        parsed = _parse_credentials(text)
        url = _page_url(text)
        if parsed is None or url is None:
            return None
        user, password = parsed
        self._save(account_id, [_host(url)], user, password)
        cleaned = _CREDENTIALS.sub(" ", text).strip()
        return cleaned or f"open {url}"

    def available_tools(self, account_id: str) -> list[Tool]:
        if self._page_open(account_id):
            return list(self.tools)
        # Keep read so "no page is open" is a clear tool result; hide click/type until open.
        return [tool for tool in self.tools if tool.name in {"browser_open", "browser_read"}]

    def status(self, account_id: str) -> str:
        if self._page_open(account_id):
            return "browser: page open — use click/type/submit to finish website tasks here"
        return "browser: available — use browser_open for websites"

    def screenshot(self, account_id: str) -> bytes:
        if self.desk is None:
            raise RuntimeError("no live browser")
        return self.desk.screenshot(account_id)

    def live_path(self, account_id: str) -> str:
        """Relative URL the person opens to see and clear a bot wall (pixels never go to a model)."""
        if not self._page_open(account_id):
            return ""
        return f"/v1/browser/live?account_id={account_id}"

    def _page_open(self, account_id: str) -> bool:
        if self.desk is not None:
            return self.desk.has(account_id)
        return self.page is not None and account_id == self.owner

    def visible_to(self, account_id: str) -> bool:
        if self.desk is not None:
            return True
        return account_id == self.owner

    def records(self, account_id: str) -> list[dict[str, str]]:
        # Do not re-read the live page on every decide(). An open tab would make
        # ordinary chat wait on Playwright (and NER). The operator loop already
        # sends the current page when the person is browsing.
        return []

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if self.desk is None and account_id != self.owner:
            raise PermissionError(account_id)
        if tool_name == "browser_open":
            url = str(arguments.get("url", ""))
            bare = _bare_name(url) if _site_alias(url) is None else None
            # A bare word on an open page may be a choice (clinic); an [ORG_n] the person named is a site.
            if bare is not None and (arguments.get("named_site") or not self._page_open(account_id)):
                found = None
                if self.find_site is not None:
                    try:
                        found = self.find_site(account_id, bare)
                    except Exception:
                        found = None
                if found is None:
                    return (
                        f"{bare} is a name, not a web address. Find its official website "
                        "(web_search), then browser_open that host — do not continue on a "
                        "different site that is already open."
                    )
                url = found
            try:
                url = _web_url(url)
            except ValueError as exc:
                if self._page_open(account_id):
                    return (
                        f"{exc}. A page is already open — use browser_click with an Interactive ref "
                        "for the person's choice, not browser_open."
                    )
                return str(exc)
            try:
                if self.desk is not None:
                    self.desk.open(account_id, url)
                else:
                    self._current(account_id).open(url)
            except ValueError as exc:
                return str(exc)
            except Exception as exc:
                return f"could not open the page. {_open_failure(exc)}"
            try:
                if self._sign_in_if_needed(account_id, url, _active_text()):
                    asked = self._direct.pop(account_id, "")
                    return asked or self._observe(account_id)
                return self._observe(account_id)
            except Exception as exc:
                return f"could not open the page. {_open_failure(exc)}"
        try:
            if tool_name == "browser_set_checked":
                target = str(arguments.get("target", ""))
                checked = arguments.get("checked")
                if not isinstance(checked, bool):
                    raise ValueError("checked must be a boolean")
                before = self._glance(account_id)
                role, name, ref = self._resolve(account_id, target, prefer=("checkbox", "switch"))
                if role not in {"checkbox", "switch", "menuitemcheckbox", "radio"} or not ref:
                    raise RuntimeError("no current checkbox or switch matching the target")
                self._use(account_id, lambda page: page.set_checked(name, checked, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                change = _action_diff(before, after)
                if "no observable change" in change:
                    change = "outcome: already satisfied"
                return f"set checked={str(checked).lower()} on {target}\n{change}\n{after}"
            if tool_name == "browser_click":
                target = str(arguments.get("target", ""))
                before = self._glance(account_id)
                try:
                    role, name, ref = self._resolve(account_id, target, prefer=("link", "button"))
                except RuntimeError as exc:
                    return str(exc)
                try:
                    self._use(account_id, lambda page: _click(page, name, role, ref=ref))
                except Exception as exc:
                    return _action_failure(exc)
                self._settle(account_id)
                after = self._observe(account_id)
                return f"clicked {target}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_type":
                target = str(arguments.get("target", ""))
                text = str(arguments.get("text", ""))
                before = self._glance(account_id)
                try:
                    role, name, ref = self._resolve(account_id, target, prefer=("textbox", "searchbox", "combobox", "spinbutton"))
                except RuntimeError as exc:
                    return str(exc)
                try:
                    self._use(account_id, lambda page: _type_text(page, name, text, role, ref=ref))
                except Exception as exc:
                    return _action_failure(exc)
                self._settle(account_id)
                after = self._observe(account_id)
                return f"typed into {target}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_select":
                target = str(arguments.get("target", ""))
                value = str(arguments.get("value", ""))
                before = self._glance(account_id)
                try:
                    role, name, ref = self._resolve(account_id, target, prefer=("combobox",))
                except RuntimeError as exc:
                    return str(exc)
                self._use(account_id, lambda page: _select_option(page, name, value, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"selected {value} in {target}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_scroll":
                direction = str(arguments.get("direction", "down"))
                target = str(arguments.get("target", ""))
                before = self._glance(account_id)
                ref = ""
                if target:
                    try:
                        _role, _name, ref = self._resolve(account_id, target, prefer=())
                    except RuntimeError as exc:
                        return str(exc)
                    if not ref:
                        raise RuntimeError("no current scroll container matching the target")
                movement = self._use(account_id, lambda page: _scroll(page, direction, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                change = _action_diff(before, after)
                if isinstance(movement, dict):
                    if movement.get("before") == movement.get("after"):
                        change = "changed: no observable change (end of scroll region)"
                    else:
                        change = f"changed: scroll position {movement.get('after')}"
                return f"scrolled {direction}\n{change}\n{after}"
            if tool_name == "browser_press":
                key = str(arguments.get("key", ""))
                before = self._glance(account_id)
                self._use(account_id, lambda page: _press_key(page, key))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"pressed {key}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_hover":
                target = str(arguments.get("target", ""))
                before = self._glance(account_id)
                try:
                    role, name, ref = self._resolve(account_id, target, prefer=("link", "button", "menuitem"))
                except RuntimeError as exc:
                    return str(exc)
                self._use(account_id, lambda page: _hover(page, name, role, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"hovered {target}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_type_focused":
                text = str(arguments.get("text", ""))
                before = self._glance(account_id)
                self._use(account_id, lambda page: _type_focused(page, text))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"typed into focused field\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_back":
                before = self._glance(account_id)
                self._use(account_id, lambda page: _go_back(page))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"went back\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_switch":
                index = int(arguments.get("index", 0))
                before = self._glance(account_id)
                self._use(account_id, lambda page: _switch_page(page, index))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"switched to page {index}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "browser_type_password":
                secret = str(arguments.get("text", ""))
                self._use(account_id, lambda page: page.type_password(secret))
                return "typed"
            if tool_name == "browser_fill_username":
                return self._fill_saved(account_id, str(arguments.get("target", "")), kind="user")
            if tool_name == "browser_fill_password":
                return self._fill_saved(account_id, str(arguments.get("target", "")), kind="password")
            if tool_name == "browser_fill_profile":
                return self._fill_profile(
                    account_id,
                    str(arguments.get("field", "")),
                    str(arguments.get("target", "")),
                )
            if tool_name == "browser_submit":
                before = self._glance(account_id)
                try:
                    self._use(account_id, lambda page: page.submit())
                except Exception as exc:
                    role, name, ref = self._submit_from_refs(account_id)
                    if not ref:
                        return _action_failure(exc)
                    try:
                        self._use(account_id, lambda page: _click(page, name, role, ref=ref))
                    except Exception as click_exc:
                        return _action_failure(click_exc)
                self._settle(account_id)
                after = self._observe(account_id)
                return f"submitted\n{_action_diff(before, after)}\n{after}"
            if tool_name in {"browser_read", "browser_find"}:
                cursor = arguments.get("cursor", 0)
                if isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor <= 2000:
                    raise ValueError("cursor must be an integer between 0 and 2000")
                query = arguments.get("query", "")
                region = arguments.get("region", "")
                if (not isinstance(query, str) or not isinstance(region, str)
                        or len(query) > 200 or region not in {"", "dialog", "main", "nav", "header", "page"}
                        or (tool_name == "browser_find" and not query.strip())):
                    raise ValueError("invalid control discovery query or region")
                return self._observe(account_id, query=query, cursor=cursor, region=region)
        except Exception as exc:
            return _action_failure(exc)
        raise NotImplementedError(tool_name)

    def _observe(self, account_id: str, *, query: str = "", cursor: int = 0, region: str = "") -> str:
        def work(page: Page) -> tuple[str, str, dict[str, tuple[str, str]], str]:
            if query or cursor or region:
                text, password = page.read(query=query, cursor=cursor, region=region)
            else:
                text, password = page.read()
            refs = dict(getattr(page, "_refs", {}) or {})
            located = ""
            if not text.startswith("URL:"):
                try:
                    located = str(page.location() or "")
                except Exception:
                    located = ""
            return text, password, refs, located

        text, password, refs, located = self._use(account_id, work)
        for secret in password.split():
            text = text.replace(secret, "")
        if not text.startswith("URL:"):
            text = _format_snapshot({"url": located, "title": "", "interactive": [], "content": text})
        if refs:
            self._refs[account_id] = refs
        else:
            self._refs[account_id] = _parse_refs(text)
        text = _hide(text, self._known_secrets(account_id) + self._once.pop(account_id, []))
        note = self._login_notes.pop(account_id, "")
        profile_note = self._profile_note(account_id, text)
        wall = _bot_wall_note(text)
        if note:
            text = f"{note}\n\n{text}"
        if profile_note:
            text = f"{profile_note}\n\n{text}"
        if wall:
            text = f"{_HANDOFF_PREFIX} {wall}\n\n{text}"
        self._last_snapshots[account_id] = text
        return text

    def _profile_note(self, account_id: str, snapshot: str) -> str:
        if self.broker is None:
            return ""
        from robin.profile import filled_keys, parse_profile, PROFILE_SECRET

        try:
            fields = parse_profile(self.broker.reveal(account_id, PROFILE_SECRET))
        except KeyError:
            return ""
        present = filled_keys(fields)
        if not present:
            return ""
        lowered = snapshot.lower()
        if not any(
            needle in lowered
            for needle in (
                "textbox",
                "email",
                "phone",
                "fornavn",
                "etternavn",
                "name",
                "navn",
                "address",
                "adresse",
            )
        ):
            return ""
        return (
            "Saved profile can fill: "
            + ", ".join(present)
            + ". Use browser_fill_profile with field and target ref — never type these values."
        )

    def _fill_profile(self, account_id: str, field: str, target: str) -> str:
        from robin.profile import normalize_field, profile_value, parse_profile, PROFILE_SECRET

        if self.broker is None:
            return "no saved profile for this account"
        try:
            fields = parse_profile(self.broker.reveal(account_id, PROFILE_SECRET))
        except KeyError:
            return "no saved profile for this account — set personal details in the app"
        key = normalize_field(field) or field.strip()
        secret = profile_value(fields, key)
        if not secret:
            label = normalize_field(field) or field.strip() or "that field"
            return f"no saved {label} in profile — ask the person or set it in the app"
        prefer = ("textbox", "searchbox", "combobox")
        needle = target.strip() or key
        try:
            role, name, ref = self._resolve(account_id, needle, prefer=prefer)
        except RuntimeError as exc:
            return str(exc)
        before = self._glance(account_id)
        try:
            self._use(
                account_id,
                lambda page: _type_text(page, name or needle, secret, role, ref=ref),
            )
        except Exception as exc:
            return _action_failure(exc)
        self._settle(account_id)
        after = self._observe(account_id)
        label = normalize_field(field) or field.strip() or "profile"
        return f"filled saved {label} into {needle}\n{_action_diff(before, after)}\n{after}"

    def _fill_saved(self, account_id: str, target: str, *, kind: str) -> str:
        url = self._url(account_id)
        hosts = _hosts(url, url)
        creds = self._load(account_id, hosts)
        if creds is None:
            return "no saved sign-in for this site"
        user, password = creds
        secret = user if kind == "user" else password
        prefer = ("textbox",) if kind == "user" else ("textbox",)
        role, name, ref = self._resolve(account_id, target, prefer=prefer)
        before = self._glance(account_id)
        try:
            if kind == "password":
                # Prefer a real password input. Snapshot may hide it or show a nearby
                # show-password toggle as button "Passord" / "Password".
                def fill_password(page: Page) -> None:
                    try:
                        page.type_password(secret)
                        return
                    except Exception:
                        pass
                    if target.strip():
                        _type_text(page, name or target, secret, role, ref=ref)
                        return
                    raise RuntimeError("no password field on this page")

                self._use(account_id, fill_password)
            else:
                self._use(
                    account_id,
                    lambda page: _type_text(page, name or target, secret, role, ref=ref),
                )
        except Exception as exc:
            return _action_failure(exc)
        self._settle(account_id)
        after = self._observe(account_id)
        label = "username" if kind == "user" else "password"
        return f"filled saved {label} into {target}\n{_action_diff(before, after)}\n{after}"

    def _resolve(self, account_id: str, target: str, *, prefer: tuple[str, ...]) -> tuple[str, str, str]:
        key = target.strip()
        refs = self._refs.get(account_id) or {}
        if key in refs:
            role, name = refs[key]
            return role, name, key
        if key.isdigit():
            raise RuntimeError("control ref is stale or not in the latest observation — browser_read")
        exact = [(ref, role, name) for ref, (role, name) in refs.items() if name == key]
        preferred = [item for item in exact if not prefer or item[1] in prefer]
        chosen = preferred or exact
        if len(chosen) > 1:
            listing = ", ".join(f'[{ref}] {role} "{name}"' for ref, role, name in chosen[:6])
            raise RuntimeError(
                f'"{key}" matches {len(chosen)} controls ({listing}). Use a ref number to choose one.'
            )
        if len(chosen) == 1:
            ref, role, name = chosen[0]
            return role, name, ref
        lowered = key.lower()
        fuzzy = [
            (ref, role, name)
            for ref, (role, name) in refs.items()
            if lowered and lowered in name.lower() and (not prefer or role in prefer)
        ]
        if len(fuzzy) > 1:
            listing = ", ".join(f'[{ref}] {role} "{name}"' for ref, role, name in fuzzy[:6])
            raise RuntimeError(
                f'"{key}" matches {len(fuzzy)} controls ({listing}). Use a ref number to choose one.'
            )
        if len(fuzzy) == 1:
            ref, role, name = fuzzy[0]
            return role, name, ref
        generic = _generic_field_match(key, refs, prefer=prefer)
        if generic is not None:
            return generic
        return "", key, ""

    def _submit_from_refs(self, account_id: str) -> tuple[str, str, str]:
        """Pick the best Interactive submit-like button when type=submit is missing."""
        refs = self._refs.get(account_id) or {}
        best: tuple[str, str, str] | None = None
        best_score = 0
        for ref, (role, name) in refs.items():
            if role != "button":
                continue
            score = _submit_score(name)
            if score > best_score:
                best_score = score
                best = (role, name, ref)
        return best or ("", "", "")

    def _url(self, account_id: str) -> str:
        try:
            return str(self._use(account_id, lambda page: page.location()) or "")
        except Exception:
            return ""

    def _glance(self, account_id: str) -> dict[str, Any]:
        def work(page: Page) -> dict[str, Any]:
            url = ""
            try:
                url = str(page.location() or "")
            except Exception:
                url = ""
            pages = 1
            listing = getattr(page, "page_list", None)
            if callable(listing):
                try:
                    pages = max(1, len(listing()))
                except Exception:
                    pages = 1
            downloads = 0
            downs = getattr(page, "downloads", None)
            if callable(downs):
                try:
                    downloads = len(downs())
                except Exception:
                    downloads = 0
            snapshot = self._last_snapshots.get(account_id)
            if isinstance(page, PlaywrightPage):
                snapshot, _password = page.read()
            return {"url": url, "pages": pages, "downloads": downloads, "snapshot": snapshot}

        try:
            return self._use(account_id, work)
        except Exception:
            return {"url": "", "pages": 1, "downloads": 0}

    def _settle(self, account_id: str) -> None:
        self._use(account_id, lambda page: _settle(page))

    def _use(self, account_id: str, function: Any) -> Any:
        if self.desk is not None:
            return self.desk.run(account_id, function)
        return function(self._current(account_id))

    def _current(self, account_id: str) -> Page:
        if self.desk is not None:
            page = self.desk.pages.get(account_id)
            if page is None:
                raise RuntimeError("no page is open")
            return page
        if self.page is None or account_id != self.owner:
            raise PermissionError(account_id)
        return self.page

    def _sign_in_if_needed(self, account_id: str, url: str, task: str) -> bool:
        if self.broker is None:
            return False
        try:
            self._use(account_id, lambda page: _reach_login(page, task))
            needed = bool(self._use(account_id, lambda page: page.needs_login()))
        except Exception:
            return False
        if not needed:
            try:
                landed = str(self._use(account_id, lambda page: page.location()))
            except Exception:
                landed = url
            return self._finish_code(account_id, _hosts(url, landed), task)
        try:
            landed = str(self._use(account_id, lambda page: page.location()))
        except Exception:
            landed = url
        hosts = _hosts(url, landed)
        creds = self._load(account_id, hosts)
        if creds is None:
            self._ask(account_id, hosts, task)
            return True
        user, password = creds
        try:
            before = landed

            def attempt(page: Page) -> str:
                page.sign_in(user, password)
                return _sign_in_result(page, before=before, timeout_ms=_SIGN_IN_WAIT_MS)

            outcome = str(self._use(account_id, attempt))
        except Exception:
            # Fall through to the adaptive operator loop with the saved secret.
            self._login_notes[account_id] = (
                "Saved sign-in exists for this site, but the automatic fill hit a browser error. "
                "Use Interactive refs with fill_saved_username and fill_saved_password."
            )
            return False
        if outcome == "ok":
            if self._finish_code(account_id, hosts, task):
                return True
            try:
                landed_after = str(self._use(account_id, lambda page: page.location()))
            except Exception:
                landed_after = landed
            self._save(account_id, _hosts(url, landed_after), user, password)
            return False
        if outcome == "wrong":
            self._drop(account_id, hosts)
            self._ask(account_id, hosts, task, failed=True, reason="wrong")
            return True
        # Auto-fill did not finish (blocked, stuck, or a glitch). Keep the secret and
        # hand the live login form to the operator loop so the model can adapt with
        # fill_saved_username / fill_saved_password on this site's refs.
        try:
            landed_after = str(self._use(account_id, lambda page: page.location()))
        except Exception:
            landed_after = landed
        self._save(account_id, _hosts(url, landed_after), user, password)
        self._login_notes[account_id] = (
            "Saved sign-in exists for this site, but the automatic fill did not finish. "
            "Use Interactive refs with fill_saved_username and fill_saved_password, "
            "click Continue or Next between steps when needed, then submit. "
            "Do not invent the password."
        )
        return False

    def _finish_code(self, account_id: str, hosts: list[str], task: str) -> bool:
        if not self._needs_code(account_id):
            return False
        turn = current_task.get()
        conversation_id = turn.conversation_id if isinstance(turn, ActiveTurn) else ""
        code = self._codes.pop((account_id, conversation_id), None) if conversation_id else None
        if not code:
            self._ask(account_id, hosts, task, kind="code")
            return True
        try:
            self._use(account_id, lambda page: page.submit_code(code))
            again = self._needs_code(account_id)
        except Exception:
            again = True
        if again:
            self._ask(account_id, hosts, task, failed=True, kind="code")
            return True
        self._once.setdefault(account_id, []).append(code)
        return False

    def _needs_code(self, account_id: str) -> bool:
        try:
            return bool(self._use(account_id, lambda page: page.needs_code()))
        except Exception:
            return False

    def _screen(self, account_id: str) -> str:
        return self._observe(account_id)

    def _ask(
        self,
        account_id: str,
        hosts: list[str],
        task: str,
        *,
        failed: bool = False,
        kind: str = "password",
        reason: str = "",
        detail: str = "",
    ) -> None:
        turn = current_task.get()
        conversation_id = turn.conversation_id if isinstance(turn, ActiveTurn) else ""
        record = {
            "hosts": hosts,
            "task": task,
            "kind": kind,
            "failed": failed,
            "request_id": f"browser-{conversation_id or 'open'}-{kind}",
            "allow_cloud": bool(turn.allow_cloud) if isinstance(turn, ActiveTurn) else False,
            "free_text": bool(turn.free_text) if isinstance(turn, ActiveTurn) else False,
        }
        if conversation_id:
            self._waits[(account_id, conversation_id)] = record
            if self.broker is not None:
                self.broker.put(account_id, _wait_name(conversation_id), json.dumps(record, sort_keys=True))
        host = hosts[0] if hosts else "that site"
        if kind == "code":
            self._direct[account_id] = _ask_code(host, failed=failed)
        else:
            self._direct[account_id] = _ask_text(host, failed=failed, reason=reason, detail=detail)

    def _wait(self, account_id: str, conversation_id: str) -> dict[str, Any] | None:
        found = self._waits.get((account_id, conversation_id))
        if found is not None:
            return found
        if self.broker is None or not conversation_id:
            return None
        try:
            raw = self.broker.reveal(account_id, _wait_name(conversation_id))
        except KeyError:
            return None
        record = json.loads(raw)
        self._waits[(account_id, conversation_id)] = record
        return record

    def _clear_wait(self, account_id: str, conversation_id: str) -> None:
        self._waits.pop((account_id, conversation_id), None)
        if self.broker is not None and conversation_id:
            self.broker.delete(account_id, _wait_name(conversation_id))

    def _save(self, account_id: str, hosts: list[str], user: str, password: str) -> None:
        if self.broker is None:
            return
        payload = json.dumps({"password": password, "user": user}, sort_keys=True)
        for host in hosts:
            if host:
                self.broker.put(account_id, f"site:{host}", payload)

    def _load(self, account_id: str, hosts: list[str]) -> tuple[str, str] | None:
        if self.broker is None:
            return None
        for host in hosts:
            try:
                raw = self.broker.reveal(account_id, f"site:{host}")
            except KeyError:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            user = str(data.get("user", ""))
            password = str(data.get("password", ""))
            if user and password:
                return user, password
        return None

    def _drop(self, account_id: str, hosts: list[str]) -> None:
        if self.broker is None:
            return
        for host in hosts:
            self.broker.delete(account_id, f"site:{host}")

    def _known_secrets(self, account_id: str) -> list[str]:
        if self.broker is None:
            return []
        found: list[str] = []
        from robin.profile import PROFILE_SECRET, parse_profile, secret_values

        for name in self.broker.names(account_id):
            if name == PROFILE_SECRET:
                try:
                    found.extend(secret_values(parse_profile(self.broker.reveal(account_id, name))))
                except KeyError:
                    pass
                continue
            if not name.startswith("site:"):
                continue
            try:
                data = json.loads(self.broker.reveal(account_id, name))
            except (KeyError, json.JSONDecodeError):
                continue
            found.extend(str(data.get(key, "")) for key in ("password", "user"))
        return [item for item in found if item]

    def _visible(self, account_id: str) -> tuple[str, str]:
        text, password = self._use(account_id, lambda page: page.read())
        for secret in password.split():
            text = text.replace(secret, "")
        return text, password


def _open_failure(exc: Exception) -> str:
    text = str(exc).strip()
    line = text.splitlines()[0].strip() if text else ""
    if line.startswith("Chromium is not installed"):
        return line
    if "Executable doesn't exist" in text:
        return "Chromium is not installed. Run playwright install chromium on this machine."
    return line[:180] or "the browser did not start"


def _call_timeout(method: Any, timeout_ms: int, *args: Any) -> Any:
    """Call a Playwright method with timeout when the binding accepts it."""
    try:
        return method(*args, timeout=timeout_ms)
    except TypeError:
        return method(*args)


def _click_locator(locator: Any) -> tuple[bool, str]:
    """Only report clicks that pass Playwright's normal actionability checks."""
    try:
        scroll = getattr(locator, "scroll_into_view_if_needed", None)
        if callable(scroll):
            try:
                scroll(timeout=2000)
            except TypeError:
                scroll()
            except Exception:
                pass
    except Exception:
        pass
    try:
        _call_timeout(locator.click, 5000)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _is_pointer_intercept(detail: str) -> bool:
    lower = (detail or "").lower()
    return "intercept" in lower or "subtree intercept" in lower


def _is_overlay_result(text: str) -> bool:
    lower = (text or "").lower()
    return "intercepts pointer" in lower or ("covered by" in lower and "overlay" in lower)


def _overlay_name(locator: Any) -> str:
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        return ""
    try:
        raw = evaluate(
            """(node) => {
              const r = node.getBoundingClientRect();
              const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
              if (!el || el === node || node.contains(el)) return "";
              const name = el.getAttribute("aria-label") || el.innerText || el.id
                || el.className || el.tagName;
              return String(name || "").replace(/\\s+/g, " ").trim().slice(0, 80);
            }"""
        )
    except Exception:
        return ""
    text = str(raw or "").strip()
    if text in {"True", "true", "False", "false"}:
        return ""
    return text


def _click_fragment(target: str) -> str:
    """Shorten long autocomplete / search labels for get_by_text fallbacks."""
    text = " ".join((target or "").split()).strip()
    if len(text) < 40:
        return text
    # Prefer the first option-like chunk before a repeated category list.
    for separator in (" i Bil ", " i Utstyr", " Finn flere", " ("):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
            break
    return text[:80].strip()


def _raise_if_disabled(locator: Any, target: str) -> None:
    try:
        disabled = locator.get_attribute("disabled")
        aria = locator.get_attribute("aria-disabled")
    except Exception:
        return
    if disabled is not None or aria == "true":
        raise RuntimeError(
            f'"{target}" is disabled — fill required fields first, then click again'
        )


def _action_failure(exc: Exception) -> str:
    text = str(exc).strip()
    line = text.splitlines()[0].strip() if text else ""
    if "disabled" in line.lower() and "fill required" in line.lower():
        return line[:180]
    if "gone or not clickable" in line.lower() or "could not be clicked" in line.lower():
        return line[:220]
    if "Timeout" in type(exc).__name__ or "Timeout" in text:
        if "get_by_label" in text:
            return "could not find that field on the page"
        return "the page did not respond in time"
    return line[:180] or "the browser action failed"


_CODE_SELECTOR = "input[autocomplete='one-time-code'], input[name*='otp' i], input[name*='totp' i], input[name*='mfa' i], input[id*='otp' i]"
_TWO_FACTOR = re.compile(r"\b(?:two[- ]factor|2fa|authentication code|verification code|one[- ]time code|authenticator)\b", re.IGNORECASE)
_CODE = re.compile(
    r"\b(?:(?:one[- ]time|authentication|verification|2fa|two[- ]factor|otp|mfa)\s+)?code\s*(?:is|:)?\s*([A-Za-z0-9]{4,10})\s*$",
    re.IGNORECASE,
)
_LOGIN_WORD = re.compile(r"\b(?:log\s*in|sign\s*in|login|logg\s*inn)\b", re.IGNORECASE)
_LOGIN_FAIL = re.compile(
    r"(?:incorrect|invalid|wrong)\s+(?:email|password|credentials)|"
    r"couldn't find|cannot find your|try again|login failed|sign[- ]in failed|"
    r"feil\s+(?:epassord|passord|brukernavn)|ugyldig\s+(?:epassord|passord)",
    re.IGNORECASE,
)
_LOGIN_BLOCK = re.compile(
    r"unusual activity|are you a robot|captcha|access denied|verify you are human|"
    r"automated|suspicious|try again later|something went wrong|"
    r"temporarily unavailable|not available in your|challenge|"
    r"are you a .{0,20} or a robot|px/captcha",
    re.IGNORECASE,
)
_CHALLENGE_WALL = re.compile(
    r"verify you are human|are you a human or a robot|are you a robot|"
    r"\bcaptcha\b|px/captcha|human verification|"
    r"attention required|just a moment\.\.\.|checking your browser|"
    r"cf-challenge|challenges\.cloudflare",
    re.IGNORECASE,
)


def is_handoff_result(text: str) -> bool:
    return text.lstrip().startswith(_HANDOFF_PREFIX)


def handoff_prompt(snapshot: str) -> str:
    """Person-facing text when a captcha or WAF wall needs a human."""
    return (
        "A captcha or security check is blocking the page. "
        "Open the live view, solve it, then tap Done so Robin can continue."
    )


def _bot_wall_note(snapshot: str) -> str:
    """Tell the model when the page is a real captcha/challenge wall."""
    if not _is_challenge_wall(snapshot):
        return ""
    return (
        "Bot/captcha wall — this site is blocking Robin's automated Chromium. "
        "Do not retry the same host or keep hopping flight OTAs expecting a different result. "
        "Tell the person those aggregators often block automation; offer web_search for a rough "
        "estimate, or ask them to name an airline site, or to check prices in their own browser."
    )


def _is_challenge_wall(snapshot: str) -> bool:
    """True only for tight captcha/WAF challenge copy, not empty frames or generic errors."""
    sample = snapshot
    if "\nContent:\n" in snapshot:
        sample = snapshot.split("\nContent:\n", 1)[1]
    blob = f"{snapshot[:800]}\n{sample[:2500]}"
    return bool(_CHALLENGE_WALL.search(blob))


_SIGNED_IN_PATH = re.compile(r"/(?:browse|profiles?|kids|gateway)(?:/|$|\?)", re.IGNORECASE)
_LOGIN_PATH = re.compile(r"/(?:log[\s_-]*in|sign[\s_-]*in|auth|account|session)(?:/|$|\?)", re.IGNORECASE)
_LOGIN_LABELS = ("Logg inn", "Log in", "Sign in", "Login", "Continue", "Fortsett")
_LOGIN_CONTINUE = ("Continue", "Next", "Fortsett", "Neste", "Logg inn", "Log in", "Sign in")
_LOGIN_SUBMIT = ("Logg inn", "Log in", "Sign in", "Continue", "Fortsett", "Submit", "Send")
_SIGN_IN_WAIT_MS = 12000
_USER_SELECTOR = (
    "input[type='email'], input[autocomplete='username'], "
    "input[name*='user' i], input[name*='email' i], input[name*='login' i], input[type='text']"
)


def _reach_login(page: Page, task: str) -> None:
    clear = getattr(page, "clear_gate", None)
    if clear is not None:
        try:
            clear()
        except Exception:
            pass
    if page.needs_login():
        return
    text, _password = page.read()
    asked = bool(_LOGIN_WORD.search(task))
    if not asked and not (len(text) <= 400 and _LOGIN_WORD.search(text)):
        return
    for label in _LOGIN_LABELS:
        if not _click_login_entry(page, label):
            continue
        _settle(page)
        awaiter = getattr(page, "_await_selector", None)
        if callable(awaiter):
            try:
                awaiter(f"{_USER_SELECTOR}, input[type='password']", timeout_ms=5000)
            except TypeError:
                try:
                    awaiter(f"{_USER_SELECTOR}, input[type='password']")
                except Exception:
                    pass
            except Exception:
                pass
        if page.needs_login():
            return


def _click_login_entry(page: Page, label: str) -> bool:
    """Open a sign-in form. Prefer the first matching control; ambiguity is ok here."""
    raw = getattr(page, "_page", None)
    if raw is not None:
        for role in ("button", "link"):
            try:
                control = raw.get_by_role(role, name=label)
                if int(control.count()) > 0:
                    _call_timeout(control.first.click, 5000)
                    return True
            except Exception:
                continue
    click = getattr(page, "click", None)
    if not callable(click):
        return False
    try:
        click(label)
        return True
    except RuntimeError as exc:
        # Ambiguous name: still take the first text match for navigation into login.
        if "matches" not in str(exc) or raw is None:
            return False
        try:
            control = raw.get_by_text(label, exact=True)
            if int(control.count()) == 0:
                control = raw.get_by_text(label)
            if int(control.count()) > 0:
                _call_timeout(control.first.click, 5000)
                return True
        except Exception:
            return False
        return False
    except Exception:
        return False


_FORGOT_LABEL = re.compile(r"forgot|glemt|reset\s+password|glemt\s+passord", re.IGNORECASE)
_FORM_SUBMIT_SKIP = re.compile(
    r"tilbake|back|cancel|avbryt|close|lukk|forgot|glemt|min side|bestill time",
    re.IGNORECASE,
)
_FORM_SUBMIT_LABELS = (
    "Send bestilling",
    "Send booking",
    "Send",
    "Submit",
    "Book",
    "Bestill",
    "Confirm",
    "Bekreft",
    "Place order",
    "Complete",
    "Fullfør",
)
_SUBMIT_SCORE = (
    (re.compile(r"\bsend\b", re.IGNORECASE), 10),
    (re.compile(r"\bsubmit\b", re.IGNORECASE), 10),
    (re.compile(r"\bbekreft\b|\bconfirm\b", re.IGNORECASE), 8),
    (re.compile(r"\bbook\b|\bbestill\b", re.IGNORECASE), 5),
    (re.compile(r"\bfullf[oø]r\b|\bcomplete\b|\bplace order\b", re.IGNORECASE), 7),
    (re.compile(r"\bfortsett\b|\bcontinue\b|\bnext\b|\bneste\b", re.IGNORECASE), 3),
)


def _submit_score(name: str) -> int:
    text = (name or "").strip()
    if not text or _FORM_SUBMIT_SKIP.search(text) or _FORGOT_LABEL.search(text):
        return 0
    return sum(weight for pattern, weight in _SUBMIT_SCORE if pattern.search(text))


def _ambiguous_message(target: str, count: int, kind: str) -> str:
    return f'"{target}" matches {count} {kind}s. Use a ref number from the snapshot to choose one.'


def _pick_submit_button(buttons: Any) -> Any | None:
    """Prefer a real submit control; skip forgot-password style buttons."""
    try:
        total = int(buttons.count())
    except Exception:
        return None
    if total <= 0:
        return None
    for index in range(total - 1, -1, -1):
        button = buttons.nth(index)
        try:
            text = str(button.inner_text() or "")
        except Exception:
            text = ""
        if not text:
            try:
                text = str(button.get_attribute("value") or button.get_attribute("aria-label") or "")
            except Exception:
                text = ""
        if _FORGOT_LABEL.search(text):
            continue
        return button
    return buttons.nth(total - 1)


def _await_signed_in(page: Page, *, before: str, timeout_ms: int | None = None) -> bool:
    return _sign_in_result(page, before=before, timeout_ms=timeout_ms) == "ok"


def _sign_in_result(page: Page, *, before: str, timeout_ms: int | None = None) -> str:
    """Wait after submit. Returns ok, wrong, blocked, or stuck."""
    limit = _SIGN_IN_WAIT_MS if timeout_ms is None else timeout_ms
    deadline = time.monotonic() + limit / 1000
    while True:
        try:
            landed = str(page.location() or "")
            if _looks_signed_in(landed) or (before and landed and _login_path(landed) != _login_path(before) and not page.needs_login()):
                return "ok"
            if not page.needs_login() and not _login_error(page):
                return "ok"
            if _login_blocked(page):
                return "blocked"
            if _login_error(page):
                return "wrong"
        except Exception:
            pass
        if time.monotonic() >= deadline:
            try:
                landed = str(page.location() or "")
                if _looks_signed_in(landed) or not page.needs_login():
                    return "ok"
                if _login_blocked(page):
                    return "blocked"
                if _login_error(page):
                    return "wrong"
            except Exception:
                pass
            return "stuck"
        settle = getattr(page, "settle", None)
        if callable(settle):
            try:
                settle()
            except Exception:
                pass
        pause = getattr(page, "wait_for_timeout", None)
        if callable(pause):
            try:
                pause(300)
                continue
            except Exception:
                pass
        time.sleep(0.3)


def _looks_signed_in(url: str) -> bool:
    path = urlparse(url).path or ""
    return bool(_SIGNED_IN_PATH.search(path))


def _login_path(url: str) -> str:
    parsed = urlparse(url)
    return f"{(parsed.netloc or '').lower()}{(parsed.path or '').rstrip('/').lower()}"


def _login_error(page: Page) -> bool:
    sample = _page_sample(page)
    return bool(sample and _LOGIN_FAIL.search(sample))


def _login_blocked(page: Page) -> bool:
    sample = _page_sample(page)
    return bool(sample and _LOGIN_BLOCK.search(sample))


def _page_sample(page: Page) -> str:
    """Text used to detect login errors. Prefer body/Content over Interactive refs."""
    raw = getattr(page, "_page", None)
    if raw is not None:
        try:
            return str(raw.locator("body").inner_text())[:2500]
        except Exception:
            pass
    try:
        text, _password = page.read()
    except Exception:
        return ""
    if "\nContent:\n" in text:
        return text.split("\nContent:\n", 1)[1][:2500]
    return text[:1200]


def _ask_text(host: str, *, failed: bool = False, reason: str = "", detail: str = "") -> str:
    if reason in {"blocked", "stuck"}:
        lead = (
            f"Robin could not finish signing in to {host} from this machine's browser. "
            f"Sites such as Netflix often block automated Chromium even when the password is correct. "
            f"Your password was kept."
        )
        hint = " Say cancel to stop, or ask Robin to continue without signing in."
        note = _failure_detail(detail)
        return f"{lead}{hint}{note}"
    if reason == "glitch":
        lead = f"Robin hit a browser error while signing in to {host}."
        return (
            f"{lead} Your password was kept. Reply with the username and the password to try again, "
            f"for example: username name@example.com password …. Or say cancel."
        )
    if failed:
        lead = f"That sign-in to {host} did not work."
    else:
        lead = f"Sign in to {host} is needed."
    return (
        f"{lead} Reply with the username and the password, for example: "
        f"username name@example.com password …. Or say cancel."
    )


def _failure_detail(detail: str) -> str:
    text = " ".join(detail.split())
    if not text:
        return ""
    if len(text) > 280:
        text = text[:280] + "…"
    return f" Page still shows: {text}"


def _host(url: str) -> str:
    name = (urlparse(url).hostname or "").lower()
    if name.startswith("www."):
        return name[4:]
    return name


def _hosts(requested: str, landed: str) -> list[str]:
    found: list[str] = []
    for url in (landed, requested):
        host = _host(url)
        if host and host not in found:
            found.append(host)
    return found


def _parse_credentials(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    match = _CREDENTIALS.search(stripped)
    if match is not None:
        parsed = _pair(match.group(1), match.group(2))
        if parsed is not None:
            return parsed
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) == 2:
        return _pair(lines[0], lines[1])
    return None


def _pair(user: str, password: str) -> tuple[str, str] | None:
    user = user.strip()
    password = password.strip()
    if not user or not password or any(char.isspace() for char in user):
        return None
    if user.lower() in {"and", "my", "the", "password", "passcode", "username", "email"}:
        return None
    if password in {"…", "...", ".."} or len(password) < 4:
        return None
    return user, password


def _ask_code(host: str, *, failed: bool = False) -> str:
    lead = f"That code for {host} did not work." if failed else f"{host} needs a verification code."
    return f"{lead} Reply with the code, for example: code 123456. Or say cancel."


def _parse_code(text: str) -> str | None:
    stripped = text.strip()
    only = re.fullmatch(r"[0-9]{4,8}", stripped)
    if only:
        return only.group(0)
    match = _CODE.search(stripped)
    if match is None:
        return None
    return match.group(1)


def _hide(text: str, secrets: list[str]) -> str:
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "")
    return text


def _wait_name(conversation_id: str) -> str:
    return f"login-wait:{conversation_id}"


def _web_url(url: str) -> str:
    """Require http(s) with a real host. Bare hosts like vethjem.no become https://vethjem.no."""
    cleaned = url.strip()
    alias = _site_alias(cleaned)
    if alias is not None:
        return alias
    if any(char.isspace() for char in cleaned):
        raise ValueError("url must be http or https")
    if _PLACEHOLDER.search(cleaned):
        raise ValueError(
            "url still has an unresolved placeholder — pass the exact ORG reference alone when the person "
            "named that site (confirm restores it), or a real http(s) host; do not invent another host"
        )
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        rest = cleaned.split("://", 1)[1]
        if not rest:
            raise ValueError("url must be http or https")
        host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "@" in host:
            host = host.rsplit("@", 1)[-1]
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        if not _host_ok(host):
            raise ValueError(
                "url must be a real website host — do not open booking choices "
                "(clinic, home visit) as URLs; use browser_click on the open page"
            )
        return cleaned
    if _SITE.fullmatch(cleaned.lower()) or _SITE.fullmatch(cleaned.split("/", 1)[0].lower()):
        return "https://" + cleaned
    raise ValueError("url must be http or https")


_SITE_ALIASES = {
    "google": "https://www.google.com/",
    "google.com": "https://www.google.com/",
    "www.google.com": "https://www.google.com/",
    "google flights": "https://www.google.com/travel/flights",
    "google flight": "https://www.google.com/travel/flights",
    "google travel": "https://www.google.com/travel/flights",
    "lot": "https://www.lot.com/",
    "lot.com": "https://www.lot.com/",
    "www.lot.com": "https://www.lot.com/",
    "lot polish airlines": "https://www.lot.com/",
    "polish airlines": "https://www.lot.com/",
    "skyscanner": "https://www.skyscanner.com/",
    "kayak": "https://www.kayak.com/",
    "kiwi": "https://www.kiwi.com/",
    "flybillet": "https://flybillet.no/",
    "flybillet.no": "https://flybillet.no/",
    "finn": "https://www.finn.no/",
    "finn.no": "https://www.finn.no/",
    "vg": "https://www.vg.no/",
    "vg.no": "https://www.vg.no/",
}


def _site_alias(url: str) -> str | None:
    """Map a restored org/site name (Google, LOT, …) to an https URL."""
    cleaned = url.strip().strip(".")
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        return None
    key = re.sub(r"\s+", " ", cleaned.lower())
    key = key.strip("?.!,;:\"'")
    return _SITE_ALIASES.get(key)


def _bare_name(url: str) -> str | None:
    """A site named without a domain (a store or brand name), not a URL or host."""
    cleaned = url.strip().strip("?.!,;:\"'")
    if not cleaned or "." in cleaned or "/" in cleaned or ":" in cleaned or _PLACEHOLDER.search(cleaned):
        return None
    if len(cleaned.split()) > 4 or len(cleaned) > 60:
        return None
    return cleaned


def _host_ok(host: str) -> bool:
    name = host.strip().lower().rstrip(".")
    if not name:
        return False
    if name == "localhost":
        return True
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", name):
        return True
    return _SITE.fullmatch(name) is not None


_PLACEHOLDER = REFERENCE


_MAX_INTERACTIVE = 80
_MAIN_LINK_RESERVE = 28
_LISTING_ROLES = frozenset({"link", "listitem", "article", "row", "option", "treeitem"})
_MAX_CONTENT = 3500
_VOLATILE_CONTENT = re.compile(
    r"(\b\d{1,2}:\d{2}(?::\d{2})?\b|^(advertisement|annonse)\b)",
    re.IGNORECASE,
)


def _is_main_listing(item: dict[str, Any]) -> bool:
    role = str(item.get("role") or "")
    region = str(item.get("region") or "page")
    return role in _LISTING_ROLES and region in {"main", "page"}


def _window_interactive(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder so the default 80-ref window keeps main-area listings visible."""
    if not items:
        return []
    main_links = [item for item in items if _is_main_listing(item)]
    others = [item for item in items if not _is_main_listing(item)]
    main_links.sort(key=lambda item: int(item.get("order") or 0))
    if len(items) <= _MAX_INTERACTIVE:
        return list(items)
    reserved = min(_MAIN_LINK_RESERVE, len(main_links))
    head_others = others[: max(0, _MAX_INTERACTIVE - reserved)]
    head_main = main_links[:reserved]
    used = {id(item) for item in head_others + head_main}
    tail = [item for item in others[len(head_others):] + main_links[reserved:] if id(item) not in used]
    return head_others + head_main + tail


def _stable_fingerprint(url: str, inputs: int, state: dict[str, Any]) -> str:
    """Quiet-period fingerprint that ignores clocks, live regions, and ads."""
    content_lines = [
        line for line in str(state.get("content") or "").splitlines()
        if line and not _VOLATILE_CONTENT.search(line)
    ]
    controls = []
    for item in state.get("interactive") or []:
        role = str(item.get("role") or "")
        if role in {"timer", "marquee", "log", "status"}:
            continue
        name = str(item.get("name") or "")
        if _VOLATILE_CONTENT.search(name):
            continue
        controls.append((role, name, item.get("states"), item.get("value")))
    return json.dumps({"url": url, "inputs": inputs, "content": content_lines, "controls": controls}, sort_keys=True)
_REF_LINE = re.compile(
    r'^\[(\d+)\]\s+(\w+)\s+"(.*)"(?:\s+\(([^)]*)\))?\s*$'
)

_SNAPSHOT_JS = """() => {
  const discovery = {};
  const cleanLabel = (value) => String(value || "")
    .replace(/\\s+/g, " ")
    .replace(/\\bikon av\\b/gi, "")
    .replace(/\\bicon(?:\\s+of)?\\b/gi, "")
    .replace(/\\s*,\\s*,+/g, ",")
    .replace(/^[,\\s]+|[,\\s]+$/g, "")
    .trim()
    .slice(0, 120);
  const INTERACTIVE = new Set([
    "link", "button", "textbox", "password", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
    "slider", "spinbutton", "treeitem", "tabpanel", "menu", "menubar", "toolbar", "frame",
  ]);
  const SKIP_ROLE = new Set(["presentation", "none", "generic", "Inline", "paragraph", "text"]);
  const implicitRole = (node) => {
    const tag = String(node.tagName || "").toLowerCase();
    const type = String(node.getAttribute("type") || "text").toLowerCase();
    if (tag === "a" && node.hasAttribute("href")) return "link";
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "input") {
      if (type === "submit" || type === "button" || type === "reset" || type === "image") return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") return "searchbox";
      if (type === "password") return "password";
      if (type === "hidden" || type === "file") return "";
      return "textbox";
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return node.multiple ? "listbox" : "combobox";
    if (tag === "option") return "option";
    if (tag === "progress") return "progressbar";
    if (tag === "meter") return "meter";
    if (tag === "dialog") return "dialog";
    if (tag === "nav") return "navigation";
    if (tag === "main") return "main";
    if (tag === "header") return "banner";
    if (tag === "footer") return "contentinfo";
    if (tag === "aside") return "complementary";
    if (tag === "form") return "form";
    if (tag === "img") return "img";
    if (tag === "iframe") return "frame";
    if (tag === "h1" || tag === "h2" || tag === "h3" || tag === "h4" || tag === "h5" || tag === "h6") return "heading";
    if (tag === "li") return "listitem";
    if (node.isContentEditable) return "textbox";
    return "";
  };
  const roleOf = (node) => {
    const explicit = cleanLabel(node.getAttribute("role") || "").toLowerCase();
    if (explicit) return explicit;
    return implicitRole(node);
  };
  const visible = (node) => {
    if (!node || node.nodeType !== 1) return false;
    if (node.getAttribute("aria-hidden") === "true") return false;
    try {
      const style = window.getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
      const rect = node.getBoundingClientRect();
      return rect.width > 0 || rect.height > 0 || node === document.activeElement;
    } catch (err) {
      return true;
    }
  };
  const parentOf = node => node.parentElement || node.getRootNode()?.host;
  const groupOf = node => {
    for (let parent = parentOf(node), depth = 0; parent && depth < 10; parent = parentOf(parent), depth++) {
      if (parent.tagName === "FIELDSET") {
        const legend = parent.querySelector("legend");
        if (legend) return cleanLabel(legend.textContent);
      }
      const label = parent.getAttribute("aria-label") || labeledBy(parent);
      if (label) return cleanLabel(label);
      const heading = [...parent.children].find(child => /^H[1-6]$/.test(child.tagName));
      if (heading) return cleanLabel(heading.textContent);
    }
    return "";
  };
  const regionOf = (node) => {
    for (let parent = node; parent; parent = parentOf(parent)) {
      if (parent.matches('[role="dialog"], dialog[open], [aria-modal="true"]')) return "dialog";
      if (parent.matches("header, [role='banner']")) return "header";
      if (parent.matches("nav, [role='navigation']")) return "nav";
      if (parent.matches("main, article, [role='main']")) return "main";
    }
    return "page";
  };
  const labeledBy = (node) => {
    const ids = String(node.getAttribute("aria-labelledby") || "").trim();
    if (!ids) return "";
    const parts = [];
    for (const id of ids.split(/\\s+/)) {
      const el = node.getRootNode().getElementById?.(id) || (node.ownerDocument || document).getElementById(id);
      if (el) parts.push(cleanLabel(el.innerText || el.textContent || ""));
    }
    return cleanLabel(parts.filter(Boolean).join(" "));
  };
  const associatedLabel = (node) => {
    const labelText = label => {
      const copy = label.cloneNode(true);
      copy.querySelectorAll("input,select,textarea,button").forEach(child => child.remove());
      return cleanLabel(copy.textContent || "");
    };
    if (node.id) {
      try {
        const label = node.getRootNode().querySelector('label[for="' + CSS.escape(node.id) + '"]');
        if (label) return labelText(label);
      } catch (err) {}
    }
    const parent = node.closest("label");
    if (parent) return labelText(parent);
    return "";
  };
  const neighbor = (node) => {
    const prev = node.previousElementSibling;
    if (prev) {
      const text = cleanLabel(prev.innerText || prev.getAttribute("aria-label") || "");
      if (text) return text.slice(0, 40);
    }
    const parent = node.parentElement;
    if (parent) {
      const text = cleanLabel(parent.innerText || parent.getAttribute("aria-label") || "");
      if (text) return text.slice(0, 40);
    }
    return "";
  };
  const nameOf = (node, role) => {
    const isField = role === "textbox" || role === "searchbox" || role === "combobox" || role === "password" || role === "spinbutton";
    const placeholder = cleanLabel(node.getAttribute("placeholder") || "");
    const looksPrivate = (value) => {
      const text = String(value || "");
      if (!text) return false;
      if (text.includes("@")) return true;
      if (/\\+?\\d[\\d\\s-]{6,}\\d/.test(text)) return true;
      return false;
    };
    if (role === "frame" || String(node.tagName || "").toLowerCase() === "iframe") {
      try {
        const host = node.src ? new URL(node.src, location.href).hostname : "";
        return cleanLabel(node.getAttribute("title") || node.getAttribute("aria-label") || host || "embedded frame");
      } catch (err) {
        return "embedded frame";
      }
    }
    const from = [
      node.getAttribute("aria-label"),
      labeledBy(node),
      associatedLabel(node),
      // Prefer name/id over private-looking placeholders so airlock does not turn
      // the only target string into [EMAIL_n] / [PHONE_n].
      isField && !looksPrivate(placeholder) ? placeholder : "",
      node.getAttribute("title"),
      node.getAttribute("alt"),
      node.value && (role === "button" || role === "link") ? node.value : "",
      isField ? "" : (node.innerText || node.textContent || ""),
      node.getAttribute("name"),
      node.id,
      isField && looksPrivate(placeholder) ? (role === "password" ? "password" : (/@/.test(placeholder) ? "email" : "phone")) : "",
      isField ? placeholder : "",
    ];
    for (const value of from) {
      const label = cleanLabel(value);
      if (label) return label.slice(0, 120);
    }
    const near = neighbor(node);
    return near ? 'unnamed, near "' + near.replace(/"/g, "'") + '"' : "unnamed";
  };
  const statesOf = (node) => {
    const states = [];
    if (node.disabled || node.getAttribute("aria-disabled") === "true") states.push("disabled");
    if (node.checked || node.getAttribute("aria-checked") === "true") states.push("checked");
    if (node.getAttribute("aria-checked") === "mixed") states.push("mixed");
    if (node.getAttribute("aria-expanded") === "true") states.push("expanded");
    if (node.getAttribute("aria-expanded") === "false") states.push("collapsed");
    if (node.getAttribute("aria-selected") === "true" || node.selected) states.push("selected");
    if (node.getAttribute("aria-pressed") === "true") states.push("pressed");
    if (node.getAttribute("aria-invalid") === "true" || (node.validity && !node.validity.valid)) states.push("invalid");
    if (node.getAttribute("aria-current")) states.push("current");
    if (document.activeElement === node) states.push("focused");
    return states;
  };
  const valueOf = (node, role) => {
    if (role === "password") return "";
    if (role === "textbox" || role === "searchbox" || role === "spinbutton" || role === "slider") {
      return cleanLabel(node.value || node.getAttribute("aria-valuetext") || node.getAttribute("aria-valuenow") || "");
    }
    if (role === "combobox" || role === "listbox") {
      if (node.selectedOptions && node.selectedOptions[0]) return cleanLabel(node.selectedOptions[0].text);
      return cleanLabel(node.value || "");
    }
    if (role === "frame") {
      try {
        return node.src ? new URL(node.src, location.href).hostname : "";
      } catch (err) {
        return "";
      }
    }
    return "";
  };
  const passwordToggle = (node, role) => {
    if (role !== "button") return false;
    const own = cleanLabel(node.innerText || node.textContent || node.getAttribute("aria-label") || "");
    if (own && !/^(show|hide|toggle|vis|skjul)\\b/i.test(own) && own.length > 2) return false;
    const wrap = node.closest("div, form, label, fieldset");
    return !!(wrap && wrap.querySelector("input[type='password']"));
  };
  const interactive = [];
  let nextRef = 1;
  const clearStamps = (root) => {
    if (!root || !root.querySelectorAll) return;
    for (const node of root.querySelectorAll("[data-robin-ref]")) {
      node.removeAttribute("data-robin-ref");
    }
  };
  const add = (node, role) => {
    if (!node || node.nodeType !== 1 || interactive.length >= 2000) return;
    if (!INTERACTIVE.has(role) && !(node.tabIndex >= 0 && role && !SKIP_ROLE.has(role))) return;
    if (!visible(node)) return;
    if (node.getAttribute("data-robin-ref")) return;
    if (passwordToggle(node, role)) return;
    const name = nameOf(node, role);
    const group = ["textbox", "searchbox", "spinbutton", "slider", "checkbox", "radio", "switch", "combobox", "listbox"].includes(role) ? groupOf(node) : "";
    const region = regionOf(node);
    if (discovery.query && !(name + " " + group + " " + role).toLowerCase().includes(discovery.query.toLowerCase())) return;
    if (discovery.region && discovery.region !== region) return;
    const owner = node.ownerDocument.defaultView;
    const refMap = owner.__robinControlRefs || (owner.__robinControlRefs = new WeakMap());
    const ref = refMap.get(node) || String(nextRef++);
    refMap.set(node, ref);
    try {
      node.setAttribute("data-robin-ref", ref);
    } catch (err) {}
    interactive.push({
      ref,
      role,
      name,
      region,
      states: statesOf(node),
      value: valueOf(node, role),
      group,
      viewport: node.getBoundingClientRect().top < window.innerHeight && node.getBoundingClientRect().bottom > 0,
      options: node.tagName === "SELECT" ? [...node.options].slice(0, 40).map(option => cleanLabel(option.text)) : [],
      order: interactive.length,
    });
  };
  const walkTree = (root) => {
    if (!root) return;
    const visit = (node) => {
      if (!node || node.nodeType !== 1) return;
      if (String(node.tagName || "").toLowerCase() === "iframe") return;
      const role = roleOf(node);
      if (role && !SKIP_ROLE.has(role)) add(node, role);
      else if (node.tabIndex >= 0) add(node, role || "button");
      if (node.shadowRoot) {
        clearStamps(node.shadowRoot);
        walkTree(node.shadowRoot);
      }
      for (const child of node.children || []) visit(child);
    };
    if (root.nodeType === 1) visit(root);
    else if (root.body) visit(root.body);
    else if (root.documentElement) visit(root.documentElement);
    else if (root.children) {
      for (const child of root.children) visit(child);
    }
  };
  const isCookieDialog = (node) => {
    if (!node || node.nodeType !== 1) return false;
    const id = String(node.id || "").toLowerCase();
    const cls = String(node.className || "").toLowerCase();
    const title = String(node.getAttribute("aria-label") || node.getAttribute("title") || "").toLowerCase();
    const blob = id + " " + cls + " " + title;
    return /sp_message|cookie|consent|cmp|tcf|onetrust|gdpr/.test(blob);
  };
  const isBlockingDialog = (node, results) => {
    if (!node || !visible(node) || isCookieDialog(node)) return false;
    const text = String(node.innerText || node.getAttribute("aria-label") || "").toLowerCase();
    if (node.querySelector("input[type='password']")) return true;
    if (/logg?\\s*inn|sign[\\s-]*in|passord|password|betaling|payment|checkout/.test(text)
        && node.querySelector("input,button")) return true;
    const modal = node.getAttribute("aria-modal") === "true"
      || String(node.tagName || "").toLowerCase() === "dialog";
    if (!modal) return false;
    try {
      const dr = node.getBoundingClientRect();
      const area = Math.max(0, dr.width) * Math.max(0, dr.height);
      if (area >= window.innerWidth * window.innerHeight * 0.35) return true;
      if (results) {
        const rr = results.getBoundingClientRect();
        const overlapW = Math.max(0, Math.min(dr.right, rr.right) - Math.max(dr.left, rr.left));
        const overlapH = Math.max(0, Math.min(dr.bottom, rr.bottom) - Math.max(dr.top, rr.top));
        if (overlapW * overlapH > 0.5 * Math.max(1, rr.width * rr.height)) return true;
      }
    } catch (err) {}
    return false;
  };
  const markUnreadFrame = (frame) => {
    add(frame, "frame");
    const last = interactive[interactive.length - 1];
    if (last && last.role === "frame" && !last.states.includes("unread")) last.states.push("unread");
  };
  const walkDocument = (doc) => {
    if (!doc) return;
    clearStamps(doc);
    const mainRoot = doc.querySelector("main, article, [role='main']") || doc.body;
    const blocking = [...doc.querySelectorAll('[role="dialog"], dialog[open], [aria-modal="true"]')]
      .find((node) => isBlockingDialog(node, mainRoot));
    if (blocking) walkTree(blocking);
    walkTree(doc);
    for (const frame of doc.querySelectorAll("iframe")) {
      try {
        const child = frame.contentDocument;
        if (child) walkDocument(child);
        else markUnreadFrame(frame);
      } catch (err) {
        markUnreadFrame(frame);
      }
    }
  };
  walkDocument(document);
  const mainRoot = document.querySelector("main, article, [role='main']") || document.body;
  const pageDialogs = [...document.querySelectorAll('[role="dialog"], dialog[open], [aria-modal="true"]')]
    .filter((node) => !isCookieDialog(node) && visible(node));
  const blockingDialog = pageDialogs.find((node) => isBlockingDialog(node, mainRoot));
  const contentRoot = blockingDialog
    || mainRoot
    || document.body
    || document.documentElement;
  const dismissibleDialogs = pageDialogs
    .filter((node) => node !== blockingDialog)
    .map((node) => ({
      name: cleanLabel(node.getAttribute("aria-label") || node.getAttribute("title")
        || (node.querySelector("h1,h2,h3,[role='heading']") || {}).textContent || "dialog"),
      ref: node.getAttribute("data-robin-ref") || "",
    }))
    .filter((row) => row.name);
  const vh = window.innerHeight || 800;
  const contentLines = [];
  const contentSeen = new Set();
  const listings = [];
  let moreBelow = false;
  const pushContent = (text, headingLevel) => {
    const line = headingLevel ? "#".repeat(headingLevel) + " " + text.slice(0, 200) : text.slice(0, 240);
    const key = text.toLowerCase();
    if (!text || text.length < 2 || contentSeen.has(key)) return false;
    contentSeen.add(key);
    contentLines.push(line);
    return true;
  };
  const headingLevelOf = (node) => {
    const tag = String(node.tagName || "").toLowerCase();
    let level = 0;
    if (tag.length === 2 && tag[0] === "h") level = Number(tag[1]) || 0;
    const ariaLevel = Number(node.getAttribute("aria-level") || 0);
    if (ariaLevel >= 1 && ariaLevel <= 6) level = ariaLevel;
    if (roleOf(node) === "heading" && !level) level = 2;
    return level >= 1 && level <= 6 ? level : 0;
  };
  const inViewport = (node) => {
    try {
      const rect = node.getBoundingClientRect();
      if (rect.bottom < 0) return false;
      if (rect.top > vh) {
        moreBelow = true;
        return false;
      }
      return true;
    } catch (err) {
      return false;
    }
  };
  const headingBlocks = contentRoot
    ? [...contentRoot.querySelectorAll("h1,h2,h3,h4,h5,h6,[role='heading'],[role='status'],output")]
    : [];
  for (const node of headingBlocks) {
    if (!visible(node) || !inViewport(node)) continue;
    const text = cleanLabel(node.innerText || node.textContent || "");
    pushContent(text, headingLevelOf(node));
  }
  const cleanLine = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const LISTING_CHROME = /^(pil til (venstre|høyre)|bilde \\d+ av \\d+|scale|sammenlign( biler)?|legg til som favoritt|gå til annonsen|betalt plassering|nyhet!?|annonse|kryss|previous|next|1 of \\d+|favorite|favoritt)$/i;
  const isFilterChrome = (node) => {
    try {
      if (node.closest("nav, header, aside, [role='navigation'], [role='banner'], [role='complementary'], [aria-labelledby*='filter' i], [class*='filter-list']")) {
        return true;
      }
      const host = node.closest("ul, ol, [role='list']");
      if (host && host.querySelector("input[type='checkbox'], input[type='radio']") && !host.querySelector("article, [role='article']")) {
        return true;
      }
      return false;
    } catch (err) {
      return false;
    }
  };
  const looksLikeListing = (node, text) => {
    const tag = String(node.tagName || "").toLowerCase();
    const role = roleOf(node);
    if ((tag === "article" || role === "article" || role === "row") && String(text || "").length >= 8) return true;
    if (/\\d[\\d\\s.,]*\\s*(kr|nok|€|\\$|£)/i.test(text)) return true;
    if (/\\bkr\\b/i.test(text) && /\\d{2,}/.test(text)) return true;
    if (/\\b20\\d\\d\\b/.test(text) && /\\bkm\\b/i.test(text)) return true;
    return false;
  };
  const composeListing = (node) => {
    const headingNode = node.querySelector("h1,h2,h3,h4,[class*='heading']");
    const heading = cleanLine((headingNode && (headingNode.innerText || headingNode.textContent)) || "");
    const lines = String(node.innerText || node.textContent || "")
      .split("\\n")
      .map(cleanLine)
      .filter((line) => line && line.length < 200 && !LISTING_CHROME.test(line));
    let price = "";
    for (let i = 0; i < lines.length; i++) {
      const direct = lines[i].match(/(\\d[\\d\\s.,]*)\\s*(kr|nok|€|\\$|£)/i);
      if (direct) { price = direct[0]; break; }
      if (/^(kr|nok|€|\\$|£)$/i.test(lines[i]) && i && /^\\d[\\d\\s.,]{2,}$/.test(lines[i - 1])) {
        price = lines[i - 1] + " " + lines[i];
        break;
      }
    }
    const meta = lines.find((line) => /20\\d\\d/.test(line) && /km/i.test(line)) || "";
    const subtitle = lines.find((line) => (
      line !== heading && line !== price && line !== meta
      && line.length >= 4 && line.length <= 80
    )) || "";
    const parts = [];
    const seenParts = new Set();
    for (const part of [heading, price, meta, subtitle]) {
      const key = part.toLowerCase();
      if (!part || seenParts.has(key)) continue;
      seenParts.add(key);
      parts.push(part);
    }
    if (parts.length >= 2) return parts.join(" · ").slice(0, 220);
    const useful = lines.filter((line) => !/sammenlign|favoritt|favorite|compare|skala|scale/i.test(line));
    return useful.slice(0, 6).join(" · ").slice(0, 220);
  };
  const listingSelector = "article, [role='article'], [role='listitem'], [role='row'], tr, li, a[href]";
  const taggedResults = document.querySelector("[class*='result-list'], [class*='search-result']");
  const listingRoot = (taggedResults && !isFilterChrome(taggedResults)) ? taggedResults : contentRoot;
  let resultCount = "";
  const countBlob = ((contentRoot || listingRoot || document.body).innerText || "");
  const countMatch = countBlob.match(/(\\d[\\d\\s]*)\\s*(treff|resultater|results|matches|annonser)/i);
  if (countMatch) resultCount = countMatch[0].replace(/\\s+/g, " ").trim();
  if (resultCount) pushContent(resultCount, 0);
  if (listingRoot && listingRoot.querySelectorAll) {
    for (const node of listingRoot.querySelectorAll(listingSelector)) {
      if (!visible(node)) continue;
      if (isFilterChrome(node)) continue;
      if (node.closest("nav, header, [role='navigation'], [role='banner']")) continue;
      const parentListing = node.parentElement && node.parentElement.closest
        ? node.parentElement.closest(listingSelector) : null;
      if (parentListing && parentListing !== listingRoot) continue;
      const rawText = String(node.innerText || node.textContent || "");
      const text = composeListing(node);
      if (!text || text.length < 2) continue;
      if (!looksLikeListing(node, text) && !looksLikeListing(node, rawText)) continue;
      const key = text.toLowerCase();
      if (listings.some(row => row.text.toLowerCase() === key)) continue;
      if (discovery.query && (text.toLowerCase().includes(discovery.query.toLowerCase())
          || rawText.toLowerCase().includes(discovery.query.toLowerCase()))) {
        const listingRole = roleOf(node) || (String(node.tagName || "").toLowerCase() === "a" ? "link" : "listitem");
        add(node, listingRole);
      }
      let ref = node.getAttribute("data-robin-ref") || "";
      if (!ref) {
        const near = node.querySelector("[data-robin-ref]") || node.closest("[data-robin-ref]");
        if (near) ref = near.getAttribute("data-robin-ref") || "";
      }
      listings.push({text, ref, region: regionOf(node)});
      pushContent(text, 0);
      if (listings.length >= 24) {
        moreBelow = true;
        break;
      }
    }
  }
  if (!listings.length && contentRoot && contentRoot.innerText) {
    for (const raw of String(contentRoot.innerText).split("\\n")) {
      const text = cleanLine(raw);
      if (LISTING_CHROME.test(text)) continue;
      if (!pushContent(text, 0)) continue;
      if (contentLines.length >= 60) {
        moreBelow = true;
        break;
      }
    }
  }
  for (const item of interactive) {
    if (item.role === "frame" && (item.states || []).includes("unread")) {
      pushContent("embedded frame from " + (item.value || item.name) + " — text not readable", 0);
    }
  }
  if (!moreBelow && listingRoot && listingRoot.querySelectorAll) {
    for (const node of listingRoot.querySelectorAll(listingSelector)) {
      try {
        if (visible(node) && node.getBoundingClientRect().top > vh) {
          moreBelow = true;
          break;
        }
      } catch (err) {}
    }
  }
  const renderedContent = contentLines.join("\\n");
  const content = renderedContent.split("\\n").reduce((lines, line) => {
    if (lines.join("\\n").length + line.length + 1 <= 3500) lines.push(line);
    return lines;
  }, []).join("\\n");
  if (content.length < renderedContent.length) moreBelow = true;
  const secrets = [];
  for (const node of document.querySelectorAll("input[type='password']")) {
    if (node.value) secrets.push(String(node.value));
  }
  return {
    url: location.href || "",
    title: cleanLabel(document.title || ""),
    interactive: interactive.sort((a, b) => {
      const score = item => (item.region === "dialog" ? 100 : 0)
        + (["textbox", "searchbox", "spinbutton", "checkbox", "radio", "switch", "combobox", "listbox"].includes(item.role) ? 50 : 0)
        + (item.viewport ? 20 : 0) + (/filter|sort|søk|search|alle biler/i.test(item.name) ? 30 : 0);
      return score(b) - score(a);
    }),
    discoveryLimited: interactive.length >= 2000,
    scroll: Math.round(window.scrollY),
    content,
    moreBelow,
    secrets,
    listings,
    resultCount,
    dismissibleDialogs,
  };
}"""


_PRICE_IN_TEXT = re.compile(
    r"(?P<amount>\d(?:[\d\s.,]{0,12}\d)?)\s*(?P<currency>kr|nok|€|\$|£)",
    re.IGNORECASE,
)


def _parse_listing_price(text: str) -> tuple[int, str] | None:
    match = _PRICE_IN_TEXT.search(text or "")
    if match is None:
        return None
    digits = re.sub(r"[^\d]", "", match.group("amount"))
    if not digits:
        return None
    amount = int(digits)
    if amount < 10:
        return None
    return amount, match.group("currency")


def _format_amount(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def _price_range_line(listings: list[dict[str, Any]]) -> str:
    parsed: list[tuple[int, str]] = []
    for row in listings:
        found = _parse_listing_price(str(row.get("text") or ""))
        if found:
            parsed.append(found)
    if not parsed:
        return ""
    prices = [amount for amount, _currency in parsed]
    currency = parsed[0][1]
    if any(amount >= 50_000 for amount in prices) and any(amount < 20_000 for amount in prices):
        prices = [amount for amount in prices if amount >= 20_000] or prices
    low, high = min(prices), max(prices)
    count = len(prices)
    if low == high:
        noun = "listing" if count == 1 else "listings"
        return f"Price range: {_format_amount(low)} {currency} from {count} {noun}"
    return (
        f"Price range: {_format_amount(low)}–{_format_amount(high)} {currency} "
        f"from {count} listings"
    )


def _snapshot_listings(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in data.get("listings") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            rows.append(item)
    return rows


def _format_snapshot(data: dict[str, Any]) -> str:
    url = str(data.get("url") or "").strip()
    title = str(data.get("title") or "").strip()
    pages = list(data.get("pages") or [])
    downloads = [str(item) for item in (data.get("downloads") or []) if str(item).strip()]
    windowed = _window_interactive(list(data.get("interactive") or []))
    interactive = windowed[:_MAX_INTERACTIVE]
    content, truncated = _content_lines(str(data.get("content") or ""))
    more_below = bool(data.get("more_below") or data.get("moreBelow") or truncated)
    listings = _snapshot_listings(data)
    result_count = str(data.get("result_count") or data.get("resultCount") or "").strip()
    range_line = _price_range_line(listings)
    if len(content) > _MAX_CONTENT:
        kept: list[str] = []
        for line in content.splitlines():
            if len("\n".join(kept + [line])) > _MAX_CONTENT:
                break
            kept.append(line)
        content = "\n".join(kept) or "(content omitted: excerpt exceeds its budget)"
        more_below = True
    lines = [f"URL: {url or '(unknown)'}"]
    if title:
        lines.append(f"Title: {title}")
    if isinstance(data.get("scroll"), (int, float)):
        lines.append(f"Scroll position: {data['scroll']}")
    if data.get("still_updating"):
        lines.append("Still updating")
    if result_count:
        lines.append(f"Results: {result_count}")
    elif listings:
        lines.append(f"Results: {len(listings)} listings")
    if range_line:
        lines.append(range_line)
    if listings:
        lines.append("Listings:")
        for row in listings[:24]:
            text = str(row.get("text") or "").strip()
            ref = str(row.get("ref") or "").strip()
            prefix = f"[{ref}] " if ref else ""
            lines.append(f"- {prefix}{text}")
    dialogs = list(data.get("dismissible_dialogs") or data.get("dismissibleDialogs") or [])
    if dialogs:
        names = ", ".join(
            str(item.get("name") or "dialog") for item in dialogs[:3] if isinstance(item, dict)
        )
        if names:
            lines.append(
                f"Dismissible dialog: {names} — dismiss if it hides results; Content is from main."
            )
    if len(pages) > 1 or downloads:
        lines.append("")
        lines.append("Pages:")
        if pages:
            for item in pages:
                index = item.get("index", 0)
                page_url = str(item.get("url") or "").strip() or "(unknown)"
                page_title = str(item.get("title") or "").replace('"', "'").strip()
                active = " (active)" if item.get("active") else ""
                label = f' "{page_title}"' if page_title else ""
                lines.append(f"[{index}] {page_url}{label}{active}")
        else:
            lines.append("(one)")
    if downloads:
        lines.append("")
        lines.append("Downloads:")
        for item in downloads[-8:]:
            lines.append(f"- {item}")
    lines.append("")
    lines.append("Interactive:")
    if interactive:
        control_chars = 0
        emitted = 0
        for index, item in enumerate(interactive, start=1):
            role = str(item.get("role") or "link")
            name = str(item.get("name") or "").replace('"', "'")
            group = str(item.get("group") or "").replace('"', "'")
            if group:
                name = f"{group}: {name}"
            ref = str(item.get("ref") or index)
            meta: list[str] = []
            region = str(item.get("region") or "").strip()
            if region:
                meta.append(region)
            for state in item.get("states") or ():
                text = str(state).strip()
                if text:
                    meta.append(text)
            value = str(item.get("value") or "").strip()
            if value and role != "password":
                meta.append(f"value={value.replace(chr(34), chr(39))}")
            options = item.get("options") or []
            if options:
                labels: list[str] = []
                for option in options:
                    label = str(option).replace('"', "'")
                    if len(" / ".join(labels + [label])) > 500:
                        break
                    labels.append(label)
                if labels:
                    meta.append("options=" + " / ".join(labels))
                if len(labels) < len(options):
                    meta.append("options omitted")
            suffix = f" ({', '.join(meta)})" if meta else ""
            row = f'[{ref}] {role} "{name}"{suffix}'
            if control_chars + len(row) > 6500:
                break
            lines.append(row)
            control_chars += len(row) + 1
            emitted += 1
        total = int(data.get("total_controls") or len(windowed))
        cursor = int(data.get("cursor") or 0)
        if cursor + emitted < total:
            lines.append(f"Controls omitted: {total - cursor - emitted}. browser_read cursor={cursor + emitted} or browser_find.")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("Content:")
    lines.append(content or "(empty)")
    if more_below and content:
        lines.append("(more below)")
    if data.get("discovery_limited"):
        lines.append("Discovery limit reached; inspect a narrower region.")
    return "\n".join(lines)


def _content_lines(text: str) -> tuple[str, bool]:
    lines: list[str] = []
    seen: set[str] = set()
    truncated = False
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        if len(line) < 2:
            continue
        key = line.casefold()
        if key in seen:
            continue
        if _SNAPSHOT_NOISE.search(line):
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= 80:
            truncated = True
            break
    return "\n".join(lines), truncated


_SNAPSHOT_NOISE = re.compile(
    r"^(cookie|cookies|accept all|reject all|godta alle|meny|menu|subscribe|abonner|"
    r"advertisement|annonse)\b",
    re.IGNORECASE,
)


def _parse_refs(snapshot: str) -> dict[str, tuple[str, str]]:
    refs: dict[str, tuple[str, str]] = {}
    for line in snapshot.splitlines():
        match = _REF_LINE.match(line.strip())
        if match:
            refs[match.group(1)] = (match.group(2), match.group(3))
    return refs


def _generic_field_match(
    target: str,
    refs: dict[str, tuple[str, str]],
    *,
    prefer: tuple[str, ...],
) -> tuple[str, str, str] | None:
    """Map airlock-stable labels like email/phone back to the real control."""
    key = target.strip().lower()
    predicates = {
        "email": lambda name: "@" in name or "email" in name.lower() or "e-post" in name.lower() or "epost" in name.lower(),
        "e-post": lambda name: "@" in name or "email" in name.lower() or "e-post" in name.lower() or "epost" in name.lower(),
        "phone": lambda name: bool(re.search(r"(\+?\d[\d\s-]{5,}\d)|telefon|phone|mobil|tlf", name.lower())),
        "telefon": lambda name: bool(re.search(r"(\+?\d[\d\s-]{5,}\d)|telefon|phone|mobil|tlf", name.lower())),
        "name": lambda name: "name" in name.lower() or "navn" in name.lower() or "fornavn" in name.lower(),
    }
    predicate = predicates.get(key)
    if predicate is None:
        return None
    matches = [
        (ref, role, name)
        for ref, (role, name) in refs.items()
        if predicate(name) and (not prefer or role in prefer or role in {"textbox", "searchbox", "combobox"})
    ]
    if len(matches) == 1:
        ref, role, name = matches[0]
        return role, name, ref
    if len(matches) > 1:
        listing = ", ".join(f'[{ref}] {role} "{name}"' for ref, role, name in matches[:6])
        raise RuntimeError(
            f'"{target}" matches {len(matches)} controls ({listing}). Use a ref number to choose one.'
        )
    return None


def _action_diff(before: dict[str, Any] | str, snapshot: str) -> str:
    if isinstance(before, str):
        before = {"url": before, "pages": 1, "downloads": 0}
    after_url = ""
    for line in snapshot.splitlines():
        if line.startswith("URL:"):
            after_url = line[4:].strip()
            break
    bits: list[str] = []
    before_url = str(before.get("url") or "")
    if before_url and after_url and before_url != after_url:
        bits.append(f"url {after_url}")
    after_pages = _count_pages(snapshot)
    before_pages = int(before.get("pages") or 1)
    if after_pages > before_pages:
        bits.append(f"popup opened ({after_pages} pages)")
    elif after_pages < before_pages:
        bits.append(f"page closed ({after_pages} pages)")
    after_downloads = _count_downloads(snapshot)
    before_downloads = int(before.get("downloads") or 0)
    if after_downloads > before_downloads:
        bits.append("download started")
    if " (dialog" in snapshot and " (dialog" not in str(before.get("snapshot") or ""):
        bits.append("dialog open")
    before_scroll = _snapshot_scroll(str(before.get("snapshot") or ""))
    after_scroll = _snapshot_scroll(snapshot)
    if before_scroll is not None and after_scroll is not None and before_scroll != after_scroll:
        bits.append(f"scroll position {after_scroll}")
    if "\nStill updating" in snapshot or snapshot.startswith("Still updating"):
        bits.append("still updating")
    if not bits:
        previous = before.get("snapshot")
        if isinstance(previous, str):
            def control_state(text: str) -> list[tuple[str, str, str]]:
                states = []
                for line in text.splitlines():
                    match = re.match(r'^\[\d+\]\s+(\w+)\s+"(.*)"\s+\((.*)\)$', line)
                    if not match:
                        continue
                    role, name, meta = match.groups()
                    relevant = [bit.strip() for bit in meta.split(",")
                                if bit.strip() in {"checked", "mixed", "expanded", "collapsed",
                                                   "selected", "pressed", "disabled", "invalid"}
                                or bit.strip().startswith("value=")]
                    if relevant:
                        name = re.sub(r"\s+\([\d\s,]+\)$", "", name)
                        states.append((role, name, ", ".join(relevant)))
                return sorted(states)

            def semantic(text: str) -> str:
                return "\n".join(
                    re.sub(r",?\s*\bfocused\b", "", re.sub(r"^\[\d+\]", "[ref]", line))
                    for line in text.splitlines()
                    if "Controls omitted:" not in line
                )
            if control_state(previous) != control_state(snapshot):
                bits.append("control state changed")
            else:
                bits.append("no observable change" if semantic(previous) == semantic(snapshot) else "page updated")
        else:
            bits.append("change not verified (no previous observation)")
    return "changed: " + ", ".join(bits)


def _snapshot_scroll(text: str) -> int | None:
    for line in str(text or "").splitlines():
        if line.startswith("Scroll position:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _count_pages(snapshot: str) -> int:
    if "\nPages:\n" not in snapshot:
        return 1
    section = snapshot.split("\nPages:\n", 1)[1]
    count = 0
    for line in section.splitlines():
        if line.startswith("["):
            count += 1
            continue
        if line.startswith("Interactive:") or line.startswith("Downloads:") or line.startswith("Content:"):
            break
    return count or 1


def _count_downloads(snapshot: str) -> int:
    if "\nDownloads:\n" not in snapshot:
        return 0
    section = snapshot.split("\nDownloads:\n", 1)[1]
    count = 0
    for line in section.splitlines():
        if line.startswith("- "):
            count += 1
            continue
        if line.startswith("Interactive:") or line.startswith("Content:") or line.startswith("Pages:"):
            break
    return count


def _settle(page: Page) -> None:
    settle = getattr(page, "settle", None)
    if callable(settle):
        settle()


def _click(page: Page, target: str, role: str = "", ref: str = "") -> None:
    click = getattr(page, "click")
    try:
        click(target, role=role, ref=ref)
    except TypeError:
        try:
            click(target, role=role)
        except TypeError:
            click(target)


def _type_text(page: Page, target: str, text: str, role: str = "", ref: str = "") -> None:
    type_text = getattr(page, "type_text")
    try:
        type_text(target, text, role=role, ref=ref)
    except TypeError:
        type_text(target, text)


def _select_option(page: Page, target: str, value: str, ref: str = "") -> None:
    select = getattr(page, "select_option", None)
    if callable(select):
        try:
            select(target, value, ref=ref)
            return
        except TypeError:
            select(target, value)
            return
    raise RuntimeError("select is not available")


def _scroll(page: Page, direction: str, ref: str = "") -> dict[str, Any] | None:
    scroll = getattr(page, "scroll", None)
    if callable(scroll):
        try:
            return scroll(direction, ref=ref)
        except TypeError:
            return scroll(direction)
    raise RuntimeError("scroll is not available")


def _press_key(page: Page, key: str) -> None:
    press = getattr(page, "press_key", None)
    if callable(press):
        press(key)
        return
    raise RuntimeError("press_key is not available")


def _hover(page: Page, target: str, role: str = "", ref: str = "") -> None:
    hover = getattr(page, "hover", None)
    if callable(hover):
        try:
            hover(target, role=role, ref=ref)
            return
        except TypeError:
            hover(target)
            return
    raise RuntimeError("hover is not available")


def _type_focused(page: Page, text: str) -> None:
    typed = getattr(page, "type_focused", None)
    if callable(typed):
        typed(text)
        return
    raise RuntimeError("type_focused is not available")


def _go_back(page: Page) -> None:
    back = getattr(page, "go_back", None)
    if callable(back):
        back()
        return
    raise RuntimeError("go_back is not available")


def _switch_page(page: Page, index: int) -> None:
    switch = getattr(page, "switch_page", None)
    if callable(switch):
        switch(index)
        return
    raise RuntimeError("switch_page is not available")
