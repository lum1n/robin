"""Browser on this instance. The model sees accessible text from the page."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from robin.capability import ActiveTurn, Capability, Effect, FieldClass, FieldSpec, SecretAccepted, Tool, current_task


class Page(Protocol):
    def read(self) -> tuple[str, str]: ...

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
        self._refs: dict[str, tuple[str, str]] = {}

    def open(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=25000)
        self.clear_gate()

    def read(self) -> tuple[str, str]:
        data = self._collect()
        refs: dict[str, tuple[str, str]] = {}
        for index, item in enumerate(data.get("interactive") or (), start=1):
            key = str(item.get("ref") or index)
            refs[key] = (str(item.get("role") or "link"), str(item.get("name") or ""))
        self._refs = refs
        text = _format_snapshot(data)
        secrets = list(data.get("secrets") or [])
        for secret in secrets:
            if secret:
                text = text.replace(secret, "")
        return text, " ".join(secret for secret in secrets if secret)

    def _collect(self) -> dict[str, Any]:
        evaluate = getattr(self._page, "evaluate", None)
        if callable(evaluate):
            try:
                raw = evaluate(_SNAPSHOT_JS)
                if isinstance(raw, dict):
                    return {
                        "url": str(raw.get("url") or self.location()),
                        "title": str(raw.get("title") or ""),
                        "interactive": list(raw.get("interactive") or [])[:_MAX_INTERACTIVE],
                        "content": str(raw.get("content") or ""),
                        "secrets": list(raw.get("secrets") or []),
                    }
            except Exception:
                pass
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

    def settle(self) -> None:
        page = self._page
        wait = getattr(page, "wait_for_load_state", None)
        if callable(wait):
            try:
                wait("domcontentloaded", timeout=5000)
            except Exception:
                pass
        pause = getattr(page, "wait_for_timeout", None)
        if callable(pause):
            try:
                pause(150)
            except Exception:
                pass

    def click(self, target: str, role: str = "", ref: str = "") -> None:
        if ref and self._act_ref(ref, "click"):
            return
        if role in {"link", "button"}:
            try:
                control = self._page.get_by_role(role, name=target)
                if int(control.count()) > 0:
                    control.first.click()
                    return
            except Exception:
                pass
        self._page.get_by_text(target).click()

    def type_text(self, target: str, text: str, role: str = "", ref: str = "") -> None:
        if ref and self._act_ref(ref, "fill", text):
            return
        try:
            labeled = self._page.get_by_label(target)
            if int(labeled.count()) > 0:
                labeled.first.fill(text)
                return
        except Exception:
            pass
        try:
            box = self._page.get_by_role("textbox", name=target)
            if int(box.count()) > 0:
                box.first.fill(text)
                return
        except Exception:
            pass
        self._page.get_by_label(target).fill(text)

    def type_password(self, text: str) -> None:
        self._fields("input[type='password']").first.fill(text)

    def select_option(self, target: str, value: str, ref: str = "") -> None:
        if ref and self._act_ref(ref, "select", value):
            return
        try:
            labeled = self._page.get_by_label(target)
            if int(labeled.count()) > 0:
                labeled.first.select_option(value)
                return
        except Exception:
            pass
        self._page.locator("select").first.select_option(value)

    def scroll(self, direction: str, ref: str = "") -> None:
        amount = {"up": -800, "down": 800, "top": -100000, "bottom": 100000}.get(direction.lower(), 800)
        if ref:
            selector = f'[data-robin-ref="{ref}"]'
            try:
                self._page.locator(selector).first.evaluate(
                    "(node, delta) => { node.scrollBy(0, delta); }",
                    amount,
                )
                return
            except Exception:
                pass
        self._page.evaluate(f"window.scrollBy(0, {int(amount)})")

    def press_key(self, key: str) -> None:
        self._page.keyboard.press(key)

    def go_back(self) -> None:
        self._page.go_back(wait_until="domcontentloaded", timeout=15000)

    def _act_ref(self, ref: str, action: str, text: str = "") -> bool:
        selector = f'[data-robin-ref="{ref}"]'
        frames: list[Any] = [self._page]
        frames.extend(frame for frame in (getattr(self._page, "frames", None) or ()) if frame is not None)
        for frame in frames:
            try:
                locator = frame.locator(selector)
                if int(locator.count()) == 0:
                    continue
                target = locator.first
                if action == "click":
                    target.click()
                elif action == "select":
                    target.select_option(text)
                else:
                    target.fill(text)
                return True
            except Exception:
                continue
        return False

    def submit(self) -> None:
        self._page.locator("button[type='submit'], input[type='submit']").first.click()

    def needs_login(self) -> bool:
        if int(self._fields("input[type='password']").count()) > 0:
            return True
        if int(self._fields(_USER_SELECTOR).count()) == 0:
            return False
        try:
            text = str(self._page.locator("body").inner_text())
        except Exception:
            return False
        return bool(_LOGIN_WORD.search(text))

    def type_username(self, text: str) -> None:
        field = self._fields(_USER_SELECTOR)
        field.first.fill(text)

    def clear_gate(self) -> None:
        for label in ("Accept all", "Accept", "I agree", "Allow all", "Agree", "Godta alle", "Godta"):
            try:
                control = self._page.get_by_role("button", name=label)
                if int(control.count()) > 0:
                    control.first.click(timeout=1500)
                    return
            except Exception:
                continue

    def location(self) -> str:
        return str(getattr(self._page, "url", "") or "")

    def sign_in(self, user: str, password: str) -> None:
        self.clear_gate()
        self.type_username(user)
        if int(self._fields("input[type='password']").count()) == 0:
            self._press(("Continue", "Next", "Log in", "Sign in"))
            try:
                self._fields("input[type='password']").first.wait_for(timeout=8000)
            except Exception:
                pass
        self.type_password(password)
        self._press(("Log in", "Sign in", "Continue", "Submit"))

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
        for label in labels:
            try:
                control = self._page.get_by_text(label)
                if int(control.count()) > 0:
                    control.first.click()
                    return
            except Exception:
                continue
        self.submit()

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


def open_chromium(url: str, profile: Path | None = None) -> PlaywrightPage:
    from playwright.sync_api import sync_playwright

    executable = _chromium_executable()
    if executable is None:
        raise RuntimeError("Chromium is not installed. Run playwright install chromium on this machine.")
    playwright = sync_playwright().start()
    if profile is not None:
        profile.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            executable_path=str(executable),
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        opened = PlaywrightPage(page)
        opened._playwright = playwright
        opened._context = context
        return opened
    browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
    page = browser.new_page()
    page.goto(url)
    opened = PlaywrightPage(page)
    opened._playwright = playwright
    opened._browser = browser
    return opened


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
    """One page per account. The opener runs only when a task opens a page."""

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
            _web_url(url)
            with self._lock:
                current = self.pages.get(account_id)
            if current is None:
                current = self._launch(account_id, url)
                with self._lock:
                    self.pages[account_id] = current
                return current
            current.open(url)
            return current

        return self._calls.run(work)

    def _launch(self, account_id: str, url: str) -> Page:
        profile = None
        if self.profiles is not None and account_id:
            profile = (self.profiles / account_id / "browser").resolve()
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


_SITE = re.compile(r"(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
_EXPLICIT_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


_CREDENTIALS = re.compile(
    r"(?:user(?:name)?|e-?mail|login)\s*(?:is|:)?\s*(\S+)\s+(?:and\s+)?(?:my\s+)?(?:password|passcode)\s*(?:is|:)?\s*(\S.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL = re.compile(r"^\s*(?:cancel|stop|never mind|nevermind)\s*\.?\s*$", re.IGNORECASE)


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
            name="open_page",
            description="Open an http or https page when the person asked for that page. Returns a text snapshot of the page.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="read_screen",
            description="Read the structured text snapshot of this account's page (URL, interactive refs, content).",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="click",
            description="Click an interactive ref from the snapshot (for example 1) or a visible name.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="type_text",
            description="Type into a textbox ref or labeled field that is not a password.",
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}, "text": {"type": "string"}},
                "required": ["target", "text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="select_option",
            description="Choose an option in a combobox or select ref.",
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}, "value": {"type": "string"}},
                "required": ["target", "value"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="scroll",
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
            name="press_key",
            description="Press a key such as Enter, Tab, Escape, or ArrowDown.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="go_back",
            description="Go back one page in the browser history.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="type_password",
            description="Type a password. Waits for confirmation.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            effect=Effect.EXTERNAL,
            drop_arguments=("text",),
        ),
        Tool(
            name="submit",
            description="Submit a form or send something from the screen.",
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

    def prepare(self, account_id: str, task: str) -> str:
        self._direct.pop(account_id, None)
        url = _page_url(task)
        if url is None:
            return ""
        try:
            opened = self.invoke(account_id, "open_page", {"url": url})
        except ValueError as exc:
            self._direct[account_id] = str(exc)
            return ""
        if opened.startswith("could not open"):
            self._direct[account_id] = opened
            return ""
        if self._sign_in_if_needed(account_id, url, task):
            return ""
        text = self._observe(account_id)
        if len(text) > 4000:
            text = text[:4000]
        return text

    def take_direct(self, account_id: str) -> str:
        return self._direct.pop(account_id, "")

    def accept_secret(self, account_id: str, conversation_id: str, text: str) -> SecretAccepted | None:
        waiting = self._wait(account_id, conversation_id)
        if waiting is None:
            return None
        if _CANCEL.match(text):
            self._codes.pop((account_id, conversation_id), None)
            self._clear_wait(account_id, conversation_id)
            return SecretAccepted(reply="Sign-in cancelled.")
        if waiting.get("kind") == "code":
            code = _parse_code(text)
            if code is None:
                return SecretAccepted(reply=_ask_code(waiting["hosts"][0]))
            self._codes[(account_id, conversation_id)] = code
            self._clear_wait(account_id, conversation_id)
            return SecretAccepted(
                resume=str(waiting["task"]),
                allow_cloud=bool(waiting.get("allow_cloud")),
                free_text=bool(waiting.get("free_text")),
            )
        parsed = _parse_credentials(text)
        if parsed is None:
            return SecretAccepted(reply=_ask_text(waiting["hosts"][0]))
        user, password = parsed
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

    def offered_tools(self, account_id: str, task: str) -> list[Tool]:
        if self._page_open(account_id):
            return list(self.tools)
        if _wants_page(task):
            return [tool for tool in self.tools if tool.name == "open_page"]
        return []

    def _page_open(self, account_id: str) -> bool:
        if self.desk is not None:
            return self.desk.has(account_id)
        return self.page is not None and account_id == self.owner

    def visible_to(self, account_id: str) -> bool:
        if self.desk is not None:
            return True
        return account_id == self.owner

    def records(self, account_id: str) -> list[dict[str, str]]:
        try:
            text, password = self._visible(account_id)
        except RuntimeError:
            return []
        if len(text) > 800:
            text = text[:800]
        return [{"text": text, "password": password}]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if self.desk is None and account_id != self.owner:
            raise PermissionError(account_id)
        if tool_name == "open_page":
            url = str(arguments.get("url", ""))
            try:
                if self.desk is not None:
                    self.desk.open(account_id, url)
                else:
                    _web_url(url)
                    self._current(account_id).open(url)
            except ValueError:
                raise
            except Exception as exc:
                return f"could not open the page. {_open_failure(exc)}"
            return self._observe(account_id)
        try:
            if tool_name == "click":
                target = str(arguments.get("target", ""))
                before = self._url(account_id)
                role, name, ref = self._resolve(account_id, target, prefer=("link", "button"))
                self._use(account_id, lambda page: _click(page, name, role, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"clicked {target}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "type_text":
                target = str(arguments.get("target", ""))
                text = str(arguments.get("text", ""))
                before = self._url(account_id)
                role, name, ref = self._resolve(account_id, target, prefer=("textbox",))
                self._use(account_id, lambda page: _type_text(page, name, text, role, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"typed into {target}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "select_option":
                target = str(arguments.get("target", ""))
                value = str(arguments.get("value", ""))
                before = self._url(account_id)
                role, name, ref = self._resolve(account_id, target, prefer=("combobox",))
                self._use(account_id, lambda page: _select_option(page, name, value, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"selected {value} in {target}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "scroll":
                direction = str(arguments.get("direction", "down"))
                target = str(arguments.get("target", ""))
                before = self._url(account_id)
                ref = ""
                if target:
                    _role, _name, ref = self._resolve(account_id, target, prefer=())
                self._use(account_id, lambda page: _scroll(page, direction, ref=ref))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"scrolled {direction}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "press_key":
                key = str(arguments.get("key", ""))
                before = self._url(account_id)
                self._use(account_id, lambda page: _press_key(page, key))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"pressed {key}\n{_action_diff(before, after)}\n{after}"
            if tool_name == "go_back":
                before = self._url(account_id)
                self._use(account_id, lambda page: _go_back(page))
                self._settle(account_id)
                after = self._observe(account_id)
                return f"went back\n{_action_diff(before, after)}\n{after}"
            if tool_name == "type_password":
                secret = str(arguments.get("text", ""))
                self._use(account_id, lambda page: page.type_password(secret))
                return "typed"
            if tool_name == "submit":
                before = self._url(account_id)
                self._use(account_id, lambda page: page.submit())
                self._settle(account_id)
                after = self._observe(account_id)
                return f"submitted\n{_action_diff(before, after)}\n{after}"
            if tool_name == "read_screen":
                return self._observe(account_id)
        except RuntimeError as exc:
            return str(exc)
        raise NotImplementedError(tool_name)

    def _observe(self, account_id: str) -> str:
        text, password = self._use(account_id, lambda page: page.read())
        for secret in password.split():
            text = text.replace(secret, "")
        page = self._current(account_id)
        refs = dict(getattr(page, "_refs", {}) or {})
        if not text.startswith("URL:"):
            try:
                located = str(page.location() or "")
            except Exception:
                located = ""
            text = _format_snapshot({"url": located, "title": "", "interactive": [], "content": text})
        if refs:
            self._refs[account_id] = refs
        else:
            self._refs[account_id] = _parse_refs(text)
        text = _hide(text, self._known_secrets(account_id) + self._once.pop(account_id, []))
        if len(text) > 4000:
            text = text[:4000]
        return text

    def _resolve(self, account_id: str, target: str, *, prefer: tuple[str, ...]) -> tuple[str, str, str]:
        key = target.strip()
        refs = self._refs.get(account_id) or {}
        if key in refs:
            role, name = refs[key]
            return role, name, key
        for ref, (role, name) in refs.items():
            if name == key and (not prefer or role in prefer):
                return role, name, ref
        for ref, (role, name) in refs.items():
            if name == key:
                return role, name, ref
        return "", key, ""

    def _url(self, account_id: str) -> str:
        try:
            return str(self._use(account_id, lambda page: page.location()) or "")
        except Exception:
            return ""

    def _settle(self, account_id: str) -> None:
        try:
            self._use(account_id, lambda page: _settle(page))
        except Exception:
            return

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
            self._use(account_id, lambda page: page.sign_in(user, password))
            still = bool(self._use(account_id, lambda page: page.needs_login()))
        except Exception:
            self._ask(account_id, hosts, task, failed=True)
            return True
        if still:
            self._drop(account_id, hosts)
            self._ask(account_id, hosts, task, failed=True)
            return True
        if self._finish_code(account_id, hosts, task):
            return True
        try:
            landed_after = str(self._use(account_id, lambda page: page.location()))
        except Exception:
            landed_after = landed
        self._save(account_id, _hosts(url, landed_after), user, password)
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

    def _ask(self, account_id: str, hosts: list[str], task: str, *, failed: bool = False, kind: str = "password") -> None:
        turn = current_task.get()
        conversation_id = turn.conversation_id if isinstance(turn, ActiveTurn) else ""
        record = {
            "hosts": hosts,
            "task": task,
            "kind": kind,
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
            self._direct[account_id] = _ask_text(host, failed=failed)

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
        for name in self.broker.names(account_id):
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


_CODE_SELECTOR = "input[autocomplete='one-time-code'], input[name*='otp' i], input[name*='totp' i], input[name*='mfa' i], input[id*='otp' i]"
_TWO_FACTOR = re.compile(r"\b(?:two[- ]factor|2fa|authentication code|verification code|one[- ]time code|authenticator)\b", re.IGNORECASE)
_CODE = re.compile(
    r"\b(?:(?:one[- ]time|authentication|verification|2fa|two[- ]factor|otp|mfa)\s+)?code\s*(?:is|:)?\s*([A-Za-z0-9]{4,10})\s*$",
    re.IGNORECASE,
)
_LOGIN_WORD = re.compile(r"\b(?:log\s*in|sign\s*in|login)\b", re.IGNORECASE)
_LOGIN_LABELS = ("Log in", "Sign in", "Login", "Continue")
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
        try:
            page.click(label)
        except Exception:
            continue
        if page.needs_login():
            return


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


def _ask_text(host: str, *, failed: bool = False) -> str:
    lead = f"That sign-in to {host} did not work." if failed else f"Sign in to {host} is needed."
    return f"{lead} Reply with the username and the password, for example: username name@example.com password …"


def _ask_code(host: str, *, failed: bool = False) -> str:
    lead = f"That code for {host} did not work." if failed else f"{host} needs a verification code."
    return f"{lead} Reply with the code, for example: code 123456"


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


def _web_url(url: str) -> None:
    if not (url.startswith("https://") or url.startswith("http://")) or any(char.isspace() for char in url):
        raise ValueError("url must be http or https")
    if not url.split("://", 1)[1]:
        raise ValueError("url must be http or https")


_MAX_INTERACTIVE = 80
_MAX_CONTENT = 3500
_REF_LINE = re.compile(
    r'^\[(\d+)\]\s+(\w+)\s+"(.*)"(?:\s+\(([^)]*)\))?\s*$'
)

_SNAPSHOT_JS = """() => {
  const cleanLabel = (value) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, 120);
  const visible = (node) => {
    if (!(node instanceof Element)) return false;
    try {
      const style = window.getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
      const rect = node.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    } catch (err) {
      return true;
    }
  };
  const regionOf = (node) => {
    if (node.closest('[role="dialog"], dialog[open], [aria-modal="true"]')) return "dialog";
    if (node.closest("header, [role='banner']")) return "header";
    if (node.closest("nav, [role='navigation']")) return "nav";
    if (node.closest("main, article, [role='main']")) return "main";
    return "page";
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
  const labelOf = (node, fallbacks) => {
    for (const value of fallbacks) {
      const label = cleanLabel(value);
      if (label) return label;
    }
    const near = neighbor(node);
    return near ? 'unnamed, near "' + near.replace(/"/g, "'") + '"' : "unnamed";
  };
  const statesOf = (node) => {
    const states = [];
    if (node.disabled || node.getAttribute("aria-disabled") === "true") states.push("disabled");
    if (node.checked || node.getAttribute("aria-checked") === "true") states.push("checked");
    if (node.getAttribute("aria-expanded") === "true") states.push("expanded");
    if (node.getAttribute("aria-selected") === "true" || node.selected) states.push("selected");
    if (node.getAttribute("aria-invalid") === "true") states.push("invalid");
    return states;
  };
  const interactive = [];
  let nextRef = 1;
  const clearStamps = (root) => {
    for (const node of root.querySelectorAll("[data-robin-ref]")) {
      node.removeAttribute("data-robin-ref");
    }
  };
  const add = (node, role, name, value) => {
    if (!visible(node) || interactive.length >= 80) return;
    if (node.getAttribute && node.getAttribute("data-robin-ref")) return;
    const ref = String(nextRef++);
    try {
      node.setAttribute("data-robin-ref", ref);
    } catch (err) {}
    interactive.push({
      ref,
      role,
      name: cleanLabel(name) || "unnamed",
      region: regionOf(node),
      states: statesOf(node),
      value: cleanLabel(value || ""),
    });
  };
  const collectIn = (scope) => {
    for (const node of scope.querySelectorAll("a[href]")) {
      add(node, "link", labelOf(node, [node.innerText, node.getAttribute("aria-label"), node.getAttribute("title")]));
    }
    for (const node of scope.querySelectorAll(
      "button, [role='button'], input[type='submit'], input[type='button'], summary"
    )) {
      add(
        node,
        "button",
        labelOf(node, [node.innerText, node.value, node.getAttribute("aria-label"), node.getAttribute("title")])
      );
    }
    for (const node of scope.querySelectorAll(
      "input:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button']):not([type='checkbox']):not([type='radio']):not([type='file']), textarea, [role='textbox']"
    )) {
      add(
        node,
        "textbox",
        labelOf(node, [
          node.getAttribute("aria-label"),
          node.getAttribute("placeholder"),
          node.getAttribute("name"),
          node.id,
        ]),
        node.value
      );
    }
    for (const node of scope.querySelectorAll("input[type='checkbox'], [role='checkbox']")) {
      add(
        node,
        "checkbox",
        labelOf(node, [node.getAttribute("aria-label"), node.getAttribute("name"), node.id, node.value])
      );
    }
    for (const node of scope.querySelectorAll("input[type='radio'], [role='radio']")) {
      add(
        node,
        "radio",
        labelOf(node, [node.getAttribute("aria-label"), node.getAttribute("name"), node.id, node.value])
      );
    }
    for (const node of scope.querySelectorAll("select, [role='combobox'], [role='listbox']")) {
      const selected = node.selectedOptions && node.selectedOptions[0] ? node.selectedOptions[0].text : node.value;
      add(
        node,
        "combobox",
        labelOf(node, [node.getAttribute("aria-label"), node.getAttribute("name"), node.id]),
        selected
      );
    }
    for (const node of scope.querySelectorAll("[role='tab'], [role='menuitem'], [role='option'], [role='switch']")) {
      const role = node.getAttribute("role") || "button";
      add(node, role, labelOf(node, [node.innerText, node.getAttribute("aria-label"), node.getAttribute("title")]));
    }
  };
  const walkShadow = (root) => {
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) {
        clearStamps(node.shadowRoot);
        collectIn(node.shadowRoot);
        walkShadow(node.shadowRoot);
      }
    }
  };
  const walkDocument = (doc) => {
    if (!doc || !doc.querySelectorAll) return;
    clearStamps(doc);
    const dialog = doc.querySelector('[role="dialog"], dialog[open], [aria-modal="true"]');
    if (dialog) {
      collectIn(dialog);
      walkShadow(dialog);
    }
    collectIn(doc);
    walkShadow(doc);
    for (const frame of doc.querySelectorAll("iframe")) {
      try {
        const child = frame.contentDocument;
        if (child) walkDocument(child);
      } catch (err) {}
    }
  };
  walkDocument(document);
  const dialog = document.querySelector('[role="dialog"], dialog[open], [aria-modal="true"]');
  const contentRoot = dialog
    || document.querySelector("main, article, [role='main']")
    || document.body
    || document.documentElement;
  let content = contentRoot && contentRoot.innerText ? String(contentRoot.innerText) : "";
  content = content.replace(/\\r/g, "").slice(0, 3500);
  const secrets = [];
  for (const node of document.querySelectorAll("input[type='password']")) {
    if (node.value) secrets.push(String(node.value));
  }
  return {
    url: location.href || "",
    title: cleanLabel(document.title || ""),
    interactive: interactive.slice(0, 80),
    content,
    secrets,
  };
}"""


def _format_snapshot(data: dict[str, Any]) -> str:
    url = str(data.get("url") or "").strip()
    title = str(data.get("title") or "").strip()
    interactive = list(data.get("interactive") or [])[:_MAX_INTERACTIVE]
    content = _content_lines(str(data.get("content") or ""))
    if len(content) > _MAX_CONTENT:
        content = content[:_MAX_CONTENT]
    lines = [f"URL: {url or '(unknown)'}"]
    if title:
        lines.append(f"Title: {title}")
    lines.append("")
    lines.append("Interactive:")
    if interactive:
        for index, item in enumerate(interactive, start=1):
            role = str(item.get("role") or "link")
            name = str(item.get("name") or "").replace('"', "'")
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
            if value:
                meta.append(f"value={value.replace(chr(34), chr(39))}")
            suffix = f" ({', '.join(meta)})" if meta else ""
            lines.append(f'[{ref}] {role} "{name}"{suffix}')
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("Content:")
    lines.append(content or "(empty)")
    return "\n".join(lines)


def _content_lines(text: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
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
            break
    return "\n".join(lines)


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


def _action_diff(before_url: str, snapshot: str) -> str:
    after_url = ""
    for line in snapshot.splitlines():
        if line.startswith("URL:"):
            after_url = line[4:].strip()
            break
    bits: list[str] = []
    if before_url and after_url and before_url != after_url:
        bits.append(f"url {after_url}")
    if " (dialog" in snapshot:
        bits.append("dialog open")
    if not bits:
        bits.append("page updated")
    return "changed: " + ", ".join(bits)


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


def _scroll(page: Page, direction: str, ref: str = "") -> None:
    scroll = getattr(page, "scroll", None)
    if callable(scroll):
        try:
            scroll(direction, ref=ref)
            return
        except TypeError:
            scroll(direction)
            return
    raise RuntimeError("scroll is not available")


def _press_key(page: Page, key: str) -> None:
    press = getattr(page, "press_key", None)
    if callable(press):
        press(key)
        return
    raise RuntimeError("press_key is not available")


def _go_back(page: Page) -> None:
    back = getattr(page, "go_back", None)
    if callable(back):
        back()
        return
    raise RuntimeError("go_back is not available")
