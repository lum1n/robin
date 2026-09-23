"""Browser on this instance. The model sees accessible text from the page."""

from __future__ import annotations

from typing import Any, Protocol

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool


class Page(Protocol):
    def read(self) -> tuple[str, str]: ...

    def click(self, target: str) -> None: ...

    def type_text(self, target: str, text: str) -> None: ...

    def type_password(self, text: str) -> None: ...

    def submit(self) -> None: ...


class PlaywrightPage:
    """Reads accessible text from a Playwright page."""

    def __init__(self, page: Any) -> None:
        self._page = page

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


class Browser(Capability):
    id = "display"
    tools = [
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

    def __init__(self, owner: str, page: Page) -> None:
        self.owner = owner
        self.page = page

    def visible_to(self, account_id: str) -> bool:
        return account_id == self.owner

    def records(self, account_id: str) -> list[dict[str, str]]:
        if account_id != self.owner:
            return []
        text, password = self._visible()
        return [{"text": text, "password": password}]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if account_id != self.owner:
            raise PermissionError(account_id)
        if tool_name == "click":
            self.page.click(str(arguments.get("target", "")))
            return "clicked"
        if tool_name == "type_text":
            self.page.type_text(str(arguments.get("target", "")), str(arguments.get("text", "")))
            return "typed"
        if tool_name == "type_password":
            self.page.type_password(str(arguments.get("text", "")))
            return "typed"
        if tool_name == "submit":
            self.page.submit()
            return "submitted"
        if tool_name == "read_screen":
            text, _password = self._visible()
            return text
        raise NotImplementedError(tool_name)

    def _visible(self) -> tuple[str, str]:
        text, password = self.page.read()
        for secret in password.split():
            text = text.replace(secret, "")
        return text, password
