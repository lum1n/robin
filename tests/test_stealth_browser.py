"""Launch options, handoff, and live-view auth for the unblocked browser."""

from __future__ import annotations

import os

from robin.capabilities.browser import Browser, Desk, _bot_wall_note, is_handoff_result, launch_options
from robin.capabilities.vdisplay import browser_engine, headless_requested
from robin.http import Service, dispatch
from robin.loop import converse, resume
from robin.model import ModelTurn, ToolCall
from robin.policy import Task
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


class Scripted:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.seen: list[tuple[list[dict], list[str]]] = []

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.seen.append((messages, [tool["name"] for tool in tools]))
        return self.turns.pop(0)


class WallPage:
    def __init__(self) -> None:
        self.url = "https://www.skyscanner.com/"
        self.text = (
            "URL: https://www.skyscanner.com/sttc/px/captcha-v2/index.html\n"
            "Title: Skyscanner\n\nInteractive:\n(none)\n\n"
            "Content:\n# Are you a human or a robot?\nPlease don’t take this personally"
        )

    def read(self) -> tuple[str, str]:
        return self.text, ""

    def open(self, url: str) -> None:
        self.url = url

    def location(self) -> str:
        return self.url

    def click(self, target: str, role: str = "") -> None:
        return None

    def type_text(self, target: str, text: str) -> None:
        return None

    def type_password(self, text: str) -> None:
        return None

    def submit(self) -> None:
        return None

    def needs_login(self) -> bool:
        return False

    def type_username(self, text: str) -> None:
        return None

    def sign_in(self, user: str, password: str) -> None:
        return None

    def needs_code(self) -> bool:
        return False

    def submit_code(self, code: str) -> None:
        return None

    def settle(self) -> None:
        return None

    def clear_gate(self) -> None:
        return None


def test_launch_options_strip_automation_and_prefer_headed(monkeypatch) -> None:
    monkeypatch.delenv("ROBIN_BROWSER_HEADLESS", raising=False)
    monkeypatch.setattr(
        "robin.capabilities.browser._chrome_available",
        lambda: False,
    )
    options = launch_options(account_id="ada")
    assert options["headless"] is False
    assert "--enable-automation" in options["ignore_default_args"]
    assert any("AutomationControlled" in arg for arg in options["args"])
    assert options["locale"] == "nb-NO"
    assert options["timezone_id"] == "Europe/Oslo"
    assert options["viewport"]["width"] == 1920

    monkeypatch.setenv("ROBIN_BROWSER_HEADLESS", "1")
    assert launch_options()["headless"] is True
    assert headless_requested() is True


def test_browser_engine_env_and_default(monkeypatch) -> None:
    monkeypatch.setenv("ROBIN_BROWSER_ENGINE", "playwright")
    assert browser_engine() == "playwright"
    monkeypatch.setenv("ROBIN_BROWSER_ENGINE", "camoufox")
    assert browser_engine() == "camoufox"
    monkeypatch.delenv("ROBIN_BROWSER_ENGINE", raising=False)
    # Without an override, playwright is fine when patchright is not installed.
    assert browser_engine() in {"playwright", "patchright"}


def test_bot_wall_triggers_handoff_and_resume_reads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROBIN_LOG_MODEL", "0")
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(store=store)
    page = WallPage()
    assistant.add(Browser(owner="ada", page=page, broker=assistant.broker))
    model = Scripted(
        [
            ModelTurn("", (ToolCall("browser_open", {"url": "https://www.skyscanner.com/"}),)),
            ModelTurn("Here are some options after you cleared the check."),
        ]
    )
    first = converse(assistant, Task("ada", "t", "find flights", allow_cloud=True), model, max_steps=4)
    assert first.status == "handoff"
    assert first.live_url.startswith("/v1/browser/live")
    assert "captcha" in first.text.lower() or "security" in first.text.lower()
    assert is_handoff_result(
        "HANDOFF: Bot/captcha wall — this site is blocking\n\nURL: https://example/"
    )

    # Person cleared the wall: page content is usable now.
    page.text = (
        "URL: https://www.skyscanner.com/\nTitle: Flights\n\n"
        "Interactive:\n[1] searchbox \"From\"\n\nContent:\nSearch flights"
    )
    second = resume(assistant, "ada", "t", model, max_steps=4)
    assert second.status == "reply"
    assert "options" in second.text.lower() or "cleared" in second.text.lower() or second.text


def test_live_view_requires_session(tmp_path) -> None:
    from robin.auth import Auth
    from robin.model import ChatModel

    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(store=store)
    page = WallPage()
    browser = Browser(owner="ada", page=page)
    assistant.add(browser)
    auth = Auth(store)
    auth.register("ada", "secret")
    token = auth.login("ada", "secret")
    service = Service(assistant, ChatModel(transport=lambda *a, **k: None), auth=auth)

    denied = dispatch(service, "GET", "/v1/browser/live", query={"account_id": "ada"})
    assert denied[0] == 401

    ok = dispatch(
        service,
        "GET",
        "/v1/browser/live",
        query={"account_id": "ada"},
        headers={"authorization": f"Bearer {token}"},
    )
    assert ok[0] == 200
    assert ok[1]["frame_path"].startswith("/v1/browser/frame")

    other = dispatch(
        service,
        "GET",
        "/v1/browser/live",
        query={"account_id": "bea"},
        headers={"authorization": f"Bearer {token}"},
    )
    assert other[0] == 401


def test_bot_wall_note_still_flags_captcha() -> None:
    text = (
        "URL: https://www.skyscanner.com/sttc/px/captcha-v2/index.html\n"
        "Content:\n# Are you a human or a robot?\n"
    )
    assert "Bot/captcha" in _bot_wall_note(text)
