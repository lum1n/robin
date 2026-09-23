"""Browser on this instance. The model sees accessible text from the page."""

from __future__ import annotations

from typing import Any, Protocol

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool


class Page(Protocol):
    def read(self) -> tuple[str, str]: ...

    def open(self, url: str) -> None: ...

    def click(self, target: str) -> None: ...

    def type_text(self, target: str, text: str) -> None: ...

    def type_password(self, text: str) -> None: ...

    def submit(self) -> None: ...


class PlaywrightPage:
    """Reads accessible text from a Playwright page."""

    def __init__(self, page: Any) -> None:
        self._page = page

    def open(self, url: str) -> None:
        self._page.goto(url)

    def read(self) -> tuple[str, str]:
        text = str(self._page.locator("body").inner_text())
        fields = self._page.locator("input[type='password']")
        values = [str(fields.nth(index).input_value()) for index in range(int(fields.count()))]
        return text, " ".join(value for value in values if value)

    def click(self, target: str) -> None:
        self._page.get_by_text(target).click()

    def type_text(self, target: str, text: str) -> None:
        self._page.get_by_label(target).fill(text)

    def type_password(self, text: str) -> None:
        self._page.locator("input[type='password']").fill(text)

    def submit(self) -> None:
        self._page.locator("button[type='submit'], input[type='submit']").first.click()


def open_chromium(url: str) -> PlaywrightPage:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(url)
    opened = PlaywrightPage(page)
    opened._playwright = playwright
    opened._browser = browser
    return opened


class Desk:
    """One page per account. The opener runs only when a task opens a page."""

    def __init__(self, opener: Any) -> None:
        self.opener = opener
        self.pages: dict[str, Page] = {}

    def open(self, account_id: str, url: str) -> Page:
        _web_url(url)
        current = self.pages.get(account_id)
        if current is None:
            current = self.opener(url)
            self.pages[account_id] = current
            return current
        current.open(url)
        return current


class Browser(Capability):
    id = "display"
    tools = [
        Tool(
            name="open_page",
            description="Open an http or https page for this account.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="read_screen",
            description="Read the text of this account's screen.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="click",
            description="Click within the task the person just gave.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
            effect=Effect.MUTATE,
        ),
        Tool(
            name="type_text",
            description="Type into a field that is not a password.",
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}, "text": {"type": "string"}},
                "required": ["target", "text"],
            },
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

    def __init__(self, owner: str | None = None, page: Page | None = None, *, desk: Desk | None = None) -> None:
        if desk is None and (owner is None or page is None):
            raise ValueError("browser needs a page or a desk")
        self.owner = owner
        self.page = page
        self.desk = desk

    def visible_to(self, account_id: str) -> bool:
        if self.desk is not None:
            return True
        return account_id == self.owner

    def records(self, account_id: str) -> list[dict[str, str]]:
        try:
            text, password = self._visible(account_id)
        except RuntimeError:
            return []
        return [{"text": text, "password": password}]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if self.desk is None and account_id != self.owner:
            raise PermissionError(account_id)
        if tool_name == "open_page":
            url = str(arguments.get("url", ""))
            if self.desk is not None:
                self.desk.open(account_id, url)
            else:
                _web_url(url)
                self._current(account_id).open(url)
            return "opened"
        page = self._current(account_id)
        if tool_name == "click":
            page.click(str(arguments.get("target", "")))
            return "clicked"
        if tool_name == "type_text":
            page.type_text(str(arguments.get("target", "")), str(arguments.get("text", "")))
            return "typed"
        if tool_name == "type_password":
            page.type_password(str(arguments.get("text", "")))
            return "typed"
        if tool_name == "submit":
            page.submit()
            return "submitted"
        if tool_name == "read_screen":
            text, _password = self._visible(account_id)
            return text
        raise NotImplementedError(tool_name)

    def _current(self, account_id: str) -> Page:
        if self.desk is not None:
            page = self.desk.pages.get(account_id)
            if page is None:
                raise RuntimeError("no page is open")
            return page
        if self.page is None or account_id != self.owner:
            raise PermissionError(account_id)
        return self.page

    def _visible(self, account_id: str) -> tuple[str, str]:
        text, password = self._current(account_id).read()
        for secret in password.split():
            text = text.replace(secret, "")
        return text, password


def _web_url(url: str) -> None:
    if not (url.startswith("https://") or url.startswith("http://")) or any(char.isspace() for char in url):
        raise ValueError("url must be http or https")
    if not url.split("://", 1)[1]:
        raise ValueError("url must be http or https")
