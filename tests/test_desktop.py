from robin.capabilities.desktop import Desktop, MemoryControl, MemorySurface, _wants_desktop
from robin.capabilities.browser import Browser, _wants_page
from robin.loop import converse, _is_page_snapshot
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
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
    text = desktop.invoke("a1", "desktop_read", {})
    assert _is_page_snapshot(text)
    assert text.startswith("URL: desktop://Calculator")
    assert "Interactive:" in text
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
    desktop.invoke("a1", "desktop_read", {})
    clicked = desktop.invoke("a1", "desktop_click", {"target": "1"})
    assert surface.clicked == ["1"]
    assert "clicked 1" in clicked
    assert 'button "7"' in clicked
    typed = desktop.invoke("a1", "desktop_type", {"target": "Amount", "text": "42"})
    assert surface.typed == [("2", "42")]
    assert "typed into Amount" in typed
    assert "value=42" in typed


def test_sensitive_click_redirects_to_confirm_tools():
    surface = MemorySurface(controls=[MemoryControl("button", "Send email")])
    desktop = Desktop(surface)
    desktop.invoke("a1", "desktop_read", {})
    reply = desktop.invoke("a1", "desktop_click", {"target": "Send email"})
    assert "desktop_submit" in reply
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
    assert effects["desktop_submit"] == "external"
    assert effects["desktop_pay"] == "external"
    assert effects["desktop_delete"] == "external"
    desktop.invoke("a1", "desktop_read", {})
    desktop.invoke("a1", "desktop_submit", {"target": "1"})
    desktop.invoke("a1", "desktop_pay", {"target": "2"})
    desktop.invoke("a1", "desktop_delete", {"target": "3"})
    assert surface.clicked == ["1", "2", "3"]


def test_model_drives_desktop_without_prepare():
    class ReadyNer(UnavailableNer):
        def available(self) -> bool:
            return True

    surface = MemorySurface(controls=[MemoryControl("button", "7")], content="Calculator")
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Desktop(surface))
    model = _Script(
        [
            ModelTurn("", (ToolCall("desktop_read", {"app": "Calculator"}),)),
            ModelTurn("", (ToolCall("desktop_click", {"target": "1"}),)),
            ModelTurn("Clicked seven."),
        ]
    )
    reply = converse(assistant, Task("a1", "t", "click 7 in the Calculator window"), model)
    assert reply.status == "reply"
    assert "Clicked seven" in reply.text
    assert surface.clicked == ["1"]


def test_desktop_and_browser_tools_are_both_available():
    desktop = Desktop(MemorySurface())
    browser = Browser(owner="a1", page=_EmptyPage())
    assert {tool.name for tool in desktop.available_tools("a1")} == {tool.name for tool in desktop.tools}
    assert "browser_open" in {tool.name for tool in browser.available_tools("a1")}


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
        desktop.invoke("a1", "desktop_read", {})
    except RuntimeError as exc:
        assert "not available" in str(exc)
    else:
        raise AssertionError("expected unavailable")


class _EmptyPage:
    def read(self):
        return "URL: https://example.com\n\nInteractive:\n(none)\n\nContent:\n(empty)", ""

    def open(self, url: str) -> None:
        return None

    def location(self) -> str:
        return "https://example.com"


class _Script:
    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, *, messages, tools):
        return self.turns.pop(0)
