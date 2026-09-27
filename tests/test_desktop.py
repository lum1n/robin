from robin.capabilities.desktop import Desktop, MemoryControl, MemorySurface, _wants_desktop
from robin.capabilities.browser import Browser, _wants_page
from robin.loop import converse, _is_page_snapshot
from robin.model import ModelTurn, ToolCall
from robin.policy import Task
from robin.session import Assistant


def test_wants_desktop_not_a_website():
    assert _wants_desktop("click 7 in the Calculator window")
    assert _wants_desktop("open the Calculator app")
    assert not _wants_desktop("open https://example.com")
    assert not _wants_desktop("check vg.no")
    assert _wants_page("check vg.no")


def test_snapshot_matches_browser_shape():
    surface = MemorySurface(
        controls=[
            MemoryControl("button", "7"),
            MemoryControl("button", "Send", region="dialog"),
            MemoryControl("textbox", "Name", value="Ada"),
        ],
        content="Ready",
    )
    desktop = Desktop(surface)
    text = desktop.invoke("a1", "read_screen", {})
    assert _is_page_snapshot(text)
    assert text.startswith("URL: desktop://Calculator")
    assert 'Interactive:' in text
    assert '[1] button "7"' in text
    assert '[2] button "Send" (dialog)' in text
    assert '[3] textbox "Name"' in text
    assert "Content:\nReady" in text


def test_click_and_type_return_fresh_snapshot():
    surface = MemorySurface(
        controls=[
            MemoryControl("button", "7"),
            MemoryControl("textbox", "Amount"),
        ]
    )
    desktop = Desktop(surface)
    desktop.invoke("a1", "read_screen", {})
    clicked = desktop.invoke("a1", "click", {"target": "1"})
    assert surface.clicked == ["1"]
    assert "clicked 1" in clicked
    assert 'button "7"' in clicked
    typed = desktop.invoke("a1", "type_text", {"target": "Amount", "text": "42"})
    assert surface.typed == [("2", "42")]
    assert "typed into Amount" in typed
    assert "value=42" in typed


def test_sensitive_click_redirects_to_confirm_tools():
    surface = MemorySurface(controls=[MemoryControl("button", "Send email")])
    desktop = Desktop(surface)
    desktop.invoke("a1", "read_screen", {})
    reply = desktop.invoke("a1", "click", {"target": "Send email"})
    assert "submit" in reply
    assert surface.clicked == []


def test_submit_pay_delete_are_external():
    surface = MemorySurface(
        controls=[
            MemoryControl("button", "Send"),
            MemoryControl("button", "Pay now"),
            MemoryControl("button", "Delete"),
        ]
    )
    desktop = Desktop(surface)
    effects = {tool.name: tool.effect.value for tool in desktop.tools}
    assert effects["submit"] == "external"
    assert effects["pay"] == "external"
    assert effects["delete_item"] == "external"
    desktop.invoke("a1", "read_screen", {})
    desktop.invoke("a1", "submit", {"target": "1"})
    desktop.invoke("a1", "pay", {"target": "2"})
    desktop.invoke("a1", "delete_item", {"target": "3"})
    assert surface.clicked == ["1", "2", "3"]


def test_prepare_seeds_operator_loop():
    from robin.ner import UnavailableNer

    class ReadyNer(UnavailableNer):
        def available(self) -> bool:
            return True

    surface = MemorySurface(controls=[MemoryControl("button", "7")], content="Calculator")
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Desktop(surface))
    model = _Script(
        [
            ModelTurn("", (ToolCall("click", {"target": "1"}),)),
            ModelTurn("Clicked seven."),
        ]
    )
    reply = converse(assistant, Task("a1", "t", "click 7 in the Calculator window"), model)
    assert reply.status == "reply"
    assert "Clicked seven" in reply.text
    assert surface.clicked == ["1"]
    notes = assistant.prepare("a1", "open the Calculator app")
    assert notes.startswith("URL: desktop://")


def test_offered_tools_hide_when_page_task():
    desktop = Desktop(MemorySurface())
    browser = Browser(owner="a1", page=_EmptyPage())
    assert desktop.offered_tools("a1", "open the Calculator app")
    assert not desktop.offered_tools("a1", "open https://example.com")
    assert browser.offered_tools("a1", "open https://example.com")
    assert not browser.offered_tools("a1", "open the Calculator app")


def test_unavailable_surface_says_so():
    class Dead:
        def available(self) -> bool:
            return False

        def snapshot(self, app: str = ""):
            raise AssertionError("no")

        def click(self, ref: str) -> None:
            raise AssertionError("no")

        def type_text(self, ref: str, text: str) -> None:
            raise AssertionError("no")

        def press_key(self, key: str) -> None:
            raise AssertionError("no")

        def focus_window(self, index: int) -> None:
            raise AssertionError("no")

    desktop = Desktop(Dead())
    try:
        desktop.invoke("a1", "read_screen", {})
    except RuntimeError as exc:
        assert "not available" in str(exc)
    else:
        raise AssertionError("expected unavailable")


class _EmptyPage:
    def read(self):
        return "URL: https://example.com\n\nInteractive:\n(none)\n\nContent:\n(empty)", ""

    def open(self, url: str) -> None:
        return None

    def click(self, target: str, role: str = "", ref: str = "") -> None:
        return None

    def type_text(self, target: str, text: str, role: str = "", ref: str = "") -> None:
        return None

    def type_password(self, text: str) -> None:
        return None

    def submit(self) -> None:
        return None

    def needs_login(self) -> bool:
        return False

    def type_username(self, text: str) -> None:
        return None

    def location(self) -> str:
        return "https://example.com"

    def sign_in(self, user: str, password: str) -> None:
        return None

    def needs_code(self) -> bool:
        return False

    def submit_code(self, code: str) -> None:
        return None


class _Script:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)

    def complete(self, **kwargs):
        return self.turns.pop(0)
