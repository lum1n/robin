from urllib.error import URLError
from urllib.request import Request

from robin.airlock import VocabularyTerm
from robin.capability import Capability, Effect, Tool
from robin.capabilities.groceries import Lists
from robin.capabilities.screen import Screen
from robin.model import ChatModel, ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Route, Task
from robin.loop import converse
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

FODSELSNUMMER = "01010000110"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"
API_TOKEN = "exe1.SUPERSECRETTOKEN"


class Scripted:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.seen: list[tuple[list[dict], list[str]]] = []

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.seen.append((messages, [tool["name"] for tool in tools]))
        return self.turns.pop(0)


class StubNer(UnavailableNer):
    def available(self) -> bool:
        return True


def _user_text(messages: list[dict]) -> str:
    return "\n".join(str(message.get("content") or "") for message in messages)


def test_local_model_sees_the_name_and_a_cloud_model_sees_the_placeholder() -> None:
    local = Assistant(ner=StubNer())
    local.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    local_reference = local.vaults.get("ada", "t").token("PERSON", "Jane Doe")
    local_model = Scripted([ModelTurn(f"hello {local_reference}")])
    local_reply = converse(local, Task("ada", "t", "hello Jane Doe", allow_cloud=False), local_model)
    assert local_reply.route is Route.LOCAL
    prompt = _user_text(local_model.seen[0][0])
    assert "Jane Doe" not in prompt
    assert local_reference in prompt
    assert local_reply.text == "hello Jane Doe"

    cloud = Assistant(ner=StubNer())
    cloud.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    cloud_reference = cloud.vaults.get("ada", "t").token("PERSON", "Jane Doe")
    cloud_model = Scripted([ModelTurn(f"hello {cloud_reference}")])
    cloud_reply = converse(cloud, Task("ada", "t", "hello Jane Doe", allow_cloud=True), cloud_model)
    assert cloud_reply.route is Route.CLOUD
    prompt = _user_text(cloud_model.seen[0][0])
    assert "Jane Doe" not in prompt
    assert cloud_reference in prompt
    assert cloud_reply.text == "hello Jane Doe"


def test_a_secret_in_the_reply_is_dropped_and_never_sent() -> None:
    screen = Screen(owner="ada", text=f"id {FODSELSNUMMER}", password=SECRET)
    assistant = Assistant(ner=StubNer())
    assistant.add(screen)
    assistant.broker.put("ada", "exe", API_TOKEN)
    model = Scripted([ModelTurn(f"your id is {FODSELSNUMMER}")])
    reply = converse(assistant, Task("ada", "t", "what is on screen", allow_cloud=True), model)
    assert reply.route is Route.LOCAL
    prompt = _user_text(model.seen[0][0])
    assert FODSELSNUMMER not in prompt
    assert SECRET not in prompt
    assert API_TOKEN not in prompt
    assert FODSELSNUMMER not in reply.text
    assert SECRET not in reply.text


def test_a_read_tool_runs_and_an_external_tool_stops() -> None:
    screen = Screen(owner="ada", text="desk", password="")
    lists = Lists(members={"ada"}, shared=[{"item": "milk", "list": "groceries", "loyalty": ""}], private={})
    assistant = Assistant(ner=StubNer())
    assistant.add(screen)
    assistant.add(lists)
    model = Scripted(
        [
            ModelTurn("", (ToolCall("lists_show", {"list": "groceries"}),)),
            ModelTurn("You have milk."),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "what should I buy"), model)
    assert reply.status == "reply"
    assert reply.text == "You have milk."
    tool_messages = [message for message in model.seen[1][0] if message.get("role") == "tool"]
    assert tool_messages
    assert "milk" in tool_messages[0]["content"].casefold() or "Groceries" in tool_messages[0]["content"]

    external = Scripted([ModelTurn("", (ToolCall("screen_submit", {}),))])
    held = converse(assistant, Task("ada", "t2", "send it"), external)
    assert held.status == "confirm"
    assert held.tool == "screen_submit"
    assert screen.submitted is False
    assert len(external.seen) == 1


def test_a_hidden_tool_is_not_run() -> None:
    screen = Screen(owner="ada", text="desk", password="")
    assistant = Assistant(ner=StubNer())
    assistant.add(screen)
    model = Scripted([ModelTurn("", (ToolCall("screen_read", {}),))])
    reply = converse(assistant, Task("bea", "t", "look"), model)
    assert reply.text == "That action is not available."
    assert screen.clicked is False
    assert "screen_read" not in model.seen[0][1]


def test_tools_are_offered_without_trigger_words() -> None:
    lists = Lists(members={"ada"}, shared=[], private={})
    assistant = Assistant(ner=StubNer())
    assistant.add(lists)
    names = {tool["name"] for tool in assistant.tools("ada")}
    assert "lists_show" in names
    model = Scripted(
        [
            ModelTurn("", (ToolCall("lists_show", {"list": "groceries"}),)),
            ModelTurn("The list is empty."),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "what is left for the house"), model)
    assert reply.text == "The list is empty."
    assert "lists_show" in model.seen[0][1]


def test_the_loop_stops_after_the_step_limit() -> None:
    lists = Lists(members={"ada"}, shared=[{"item": "milk", "list": "groceries", "loyalty": ""}], private={})
    assistant = Assistant(ner=StubNer())
    assistant.add(lists)
    model = Scripted(
        [ModelTurn("", (ToolCall("lists_show", {"list": "groceries"}),)) for _ in range(4)]
        + [ModelTurn("Milk is on the list.")]
    )
    reply = converse(assistant, Task("ada", "t", "check the grocery list again"), model, max_steps=4)
    assert reply.text == "Milk is on the list."
    assert len(model.seen) == 5
    assert model.seen[-1][1] == []


def test_the_loop_nudges_when_the_same_click_keeps_failing() -> None:
    from robin.capabilities.browser import Browser

    class FailPage:
        def click(self, target: str, role: str = "", ref: str = "") -> None:
            raise RuntimeError('no clickable control matching "Bil"')

        def read(self):
            return (
                'URL: https://finn.test/\n\nInteractive:\n'
                '[22] link "Bil" (nav)\n\nContent:\nsearch',
                "",
            )

        def location(self) -> str:
            return "https://finn.test/"

        def open(self, url: str) -> None:
            return None

        def needs_login(self) -> bool:
            return False

    assistant = Assistant(ner=StubNer())
    browser = Browser(owner="ada", page=FailPage())
    browser._refs["ada"] = {"22": ("link", "Bil")}
    assistant.add(browser)
    model = Scripted(
        [ModelTurn("", (ToolCall("browser_click", {"target": "22"}),)) for _ in range(4)]
        + [ModelTurn("I could not open Bil. Try another filter.")]
    )
    reply = converse(assistant, Task("ada", "t", "find cars", allow_cloud=True), model, max_steps=4)
    assert "could not open Bil" in reply.text.lower() or "another filter" in reply.text.lower()
    nudged = any(
        "failed repeatedly" in str(message.get("content") or "")
        for messages, _tools in model.seen
        for message in messages
        if message.get("role") == "user"
    )
    assert nudged
    same_again = any(
        "do not retry these arguments" in str(message.get("content") or "").lower()
        for messages, _tools in model.seen
        for message in messages
        if message.get("role") == "tool"
    )
    assert same_again


def test_a_page_snapshot_keeps_refs_after_the_airlock() -> None:
    from robin.airlock import VocabularyTerm
    from robin.loop import _release_snapshot
    from robin.vault import Vault

    class ReadyNer(UnavailableNer):
        def available(self) -> bool:
            return True

        def detect(self, text: str):
            return ()

    snapshot = (
        'URL: https://news.test/\nTitle: News\n\nInteractive:\n'
        '[1] link "Jane Doe story" (main)\n[2] button "Save" (dialog)\n\n'
        "Content:\nJane Doe leads the front page."
    )
    vault = Vault("ada", "t")
    vocabulary = (VocabularyTerm("Jane Doe"),)
    ner = ReadyNer()
    released = _release_snapshot(snapshot, vault, vocabulary, ner)
    assert released.startswith("URL: https://news.test/")
    assert '[1] link "' in released
    assert "[UNRESOLVED]" not in released
    assert "Jane Doe" not in released
    assert '[2] button "Save"' in released
    assert "Interactive:" in released
    assert "Content:" in released


def test_new_browser_metadata_is_private_even_with_parentheses() -> None:
    from robin.loop import _release_snapshot
    from robin.vault import Vault

    vault = Vault("ada", "t")
    email = "jane@example.test"
    reference = vault.token("EMAIL", email)
    person = vault.token("PERSON", "Jane Doe")
    snapshot = (
        'URL: https://example.test/\n\nInteractive:\n'
        f'[7] combobox "Jane Doe: Recipient" (main, options=Other (work) / {email})\n\n'
        'Content:\nNo matching listings'
    )
    safe = _release_snapshot(snapshot, vault, (), StubNer())
    assert email not in safe and "Jane Doe" not in safe
    assert reference in safe and person in safe
    withheld = _release_snapshot(snapshot, vault, (), UnavailableNer())
    assert email not in withheld and "Jane Doe" not in withheld
    assert "withheld:" not in withheld
    assert "No matching listings" in withheld
    assert reference in withheld and person in withheld


def test_browser_budget_preserves_content_and_complete_references() -> None:
    from robin.loop import _bound_tool_result, _TOOL_RESULT_CHARS
    from robin.vault import Vault, REFERENCE

    reference = Vault("ada", "t").token("PERSON", "Jane Doe")
    rows = [f'[{i}] link "{reference} row {i}" (main)' for i in range(200)]
    snapshot = 'URL: https://example.test/\n\nInteractive:\n' + "\n".join(rows) + (
        '\n\nContent:\n2 verified results\n' + reference
    )
    bounded = _bound_tool_result(snapshot)
    assert len(bounded) <= _TOOL_RESULT_CHARS
    assert "2 verified results" in bounded
    assert "(output omitted" in bounded
    assert bounded.count("[PERSON_") == len(list(REFERENCE.finditer(bounded)))


def test_trimming_keeps_latest_browser_observation_and_task() -> None:
    from robin.loop import _trim_messages, _MODEL_CHARS, _size

    messages = [
        {"role": "system", "content": "Operate safely."},
        {"role": "user", "content": "GLC, exact year 2027, lowest price first."},
        {"role": "tool", "content": "old results " * 6000},
        {"role": "tool", "content": 'URL: https://example.test/\nInteractive:\n'
         '[1] combobox "Sort" (value=Lowest price)\nContent:\n2 matches'},
    ]
    latest = messages[-1]["content"]
    _trim_messages(messages)
    assert messages[2]["content"] == "[result elided]"
    assert messages[-1]["content"] == latest
    assert "exact year 2027" in messages[1]["content"]
    assert _size(messages) <= _MODEL_CHARS


def test_dependent_browser_batch_waits_for_next_observation_turn() -> None:
    from robin.capabilities.browser import Browser

    class Page:
        def __init__(self):
            self.clicked = []

        def location(self):
            return "https://example.test/"

        def read(self):
            return (
                'URL: https://example.test/\n\nInteractive:\n'
                '[1] button "First"\n[2] button "Second"\n\nContent:\nReady', ""
            )

        def click(self, target, **kwargs):
            self.clicked.append(target)

    page = Page()
    browser = Browser("ada", page)
    browser._refs["ada"] = {"1": ("button", "First"), "2": ("button", "Second")}
    assistant = Assistant(ner=StubNer())
    assistant.add(browser)
    model = Scripted([
        ModelTurn("", (
            ToolCall("browser_click", {"target": "1"}, id="first"),
            ToolCall("browser_click", {"target": "2"}, id="second"),
        )),
        ModelTurn("I need the updated observation before proceeding."),
    ])
    converse(assistant, Task("ada", "t", "Open both panels"), model)
    assert page.clicked == ["First"]
    assert any("inspect the preceding browser result" in str(message.get("content", ""))
               for message in model.seen[-1][0])


def test_type_then_enter_runs_in_one_batch() -> None:
    from robin.capabilities.browser import Browser

    class Page:
        def __init__(self) -> None:
            self.typed: list[tuple[str, str]] = []
            self.keys: list[str] = []

        def location(self) -> str:
            return "https://vy.test/"

        def read(self):
            return (
                'URL: https://vy.test/\n\nInteractive:\n'
                '[4] searchbox "To"\n\nContent:\nSearch departures',
                "",
            )

        def type_text(self, target: str, text: str, role: str = "", ref: str = "") -> None:
            self.typed.append((target, text))

        def press_key(self, key: str) -> None:
            self.keys.append(key)

    page = Page()
    browser = Browser("ada", page)
    browser._refs["ada"] = {"4": ("searchbox", "To")}
    assistant = Assistant(ner=StubNer())
    assistant.add(browser)
    model = Scripted([
        ModelTurn("", (
            ToolCall("browser_type", {"target": "4", "text": "Oslo S"}, id="type"),
            ToolCall("browser_press", {"key": "Enter"}, id="enter"),
        )),
        ModelTurn("Searching Oslo S."),
    ])
    converse(assistant, Task("ada", "t", "next train to Oslo S"), model)
    assert page.typed == [("To", "Oslo S")]
    assert page.keys == ["Enter"]


def test_scroll_position_change_is_progress() -> None:
    from robin.capabilities.browser import Browser

    class Page:
        def __init__(self) -> None:
            self.y = 0
            self.scrolls = 0

        def location(self) -> str:
            return "https://vy.test/"

        def read(self):
            return (
                f"URL: https://vy.test/\nScroll position: {self.y}\n\nInteractive:\n"
                '[1] checkbox "Filter"\n\nContent:\nOslo S 14:32',
                "",
            )

        def scroll(self, direction: str, ref: str = "") -> dict:
            self.scrolls += 1
            before = self.y
            self.y += 800
            return {"before": before, "after": self.y, "maximum": 8000}

    page = Page()
    browser = Browser("ada", page)
    assistant = Assistant(ner=StubNer())
    assistant.add(browser)
    model = Scripted([
        ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),)),
        ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),)),
        ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),)),
        ModelTurn("14:32 from Oslo S."),
    ])
    converse(assistant, Task("ada", "t", "train times"), model)
    assert page.scrolls == 3


def test_give_up_is_nudged_when_snapshot_already_has_listing_prices() -> None:
    from robin.capabilities.browser import Browser

    snapshot = (
        "URL: https://www.finn.no/mobility/search/car?q=Tesla\n"
        "Results: 224 treff\n"
        "Price range: 324 532–429 000 kr from 3 listings\n"
        "Listings:\n"
        "- Tesla Model Y · Performance AWD · 2022 · 328 532 kr\n"
        "- Tesla Model Y · Performance AWD · 2023 · 324 532 kr\n"
        "- Tesla Model Y · Performance · 2024 · 429 000 kr\n\n"
        "Interactive:\n[1] checkbox \"Tesla\" (page)\n\n"
        "Content:\nTesla Model Y · Performance AWD · 328 532 kr"
    )

    class Page:
        def location(self) -> str:
            return "https://www.finn.no/mobility/search/car?q=Tesla"

        def read(self, *, query: str = "", cursor: int = 0, region: str = ""):
            return snapshot, ""

        def open(self, url: str) -> None:
            return None

    page = Page()
    browser = Browser("ada", page)
    assistant = Assistant(ner=StubNer())
    assistant.add(browser)
    model = Scripted([
        ModelTurn("", (ToolCall("browser_read", {}),)),
        ModelTurn(
            "I couldn’t find specific listings for a 2022 Tesla Model Y Performance on the site. "
            "However, it might help to look at similar models or websites to gauge the price."
        ),
        ModelTurn(
            "Listings are on the page. Prices run from 324 532 kr to 429 000 kr "
            "(examples: 328 532 kr, 324 532 kr, 429 000 kr)."
        ),
    ])
    reply = converse(
        assistant,
        Task("ada", "t", "Find Tesla Model Y Performance listings on finn.no and a price range"),
        model,
        max_steps=6,
    )
    assert "324 532" in reply.text and "429 000" in reply.text
    assert "couldn’t find" not in reply.text.lower() and "couldn't find" not in reply.text.lower()
    nudge = "\n".join(
        str(message.get("content") or "")
        for messages, _ in model.seen
        for message in messages
        if message.get("role") == "user"
    )
    assert "already lists matching ads with prices" in nudge
    assert "2022" not in nudge or "did not ask" in nudge


def test_blocked_repeat_returns_listing_text_instead_of_ending() -> None:
    from robin.capabilities.browser import Browser

    class Page:
        def __init__(self) -> None:
            self.scrolls = 0

        def location(self) -> str:
            return "https://shop.test/"

        def read(self, *, query: str = "", cursor: int = 0, region: str = ""):
            return (
                "URL: https://shop.test/\n\nInteractive:\n"
                '[1] link "Helmelk 1L" (main)\n\nContent:\nHelmelk 1L 18 kr',
                "",
            )

        def scroll(self, direction: str, ref: str = "") -> dict:
            self.scrolls += 1
            return {"before": 0, "after": 0, "maximum": 0}

    page = Page()
    browser = Browser("ada", page)
    assistant = Assistant(ner=StubNer())
    assistant.add(browser)
    model = Scripted([
        ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),)),
        ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),)),
        ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),)),
        ModelTurn("Helmelk is 18 kr."),
    ])
    reply = converse(assistant, Task("ada", "t", "milk price"), model, max_steps=6)
    assert page.scrolls == 2
    assert "18 kr" in reply.text or any(
        "Helmelk" in str(message.get("content") or "")
        for messages, _ in model.seen
        for message in messages
    )
    assert "could not verify the requested filters" not in reply.text


def test_overlay_click_is_not_a_dead_click() -> None:
    from robin.capabilities.browser import Browser
    from robin.loop import _is_overlay_miss, _note_repeated_failure

    result = (
        'control [22] is covered by overlay "Cookie" that intercepts pointer '
        "— target [22] still current"
    )
    assert _is_overlay_miss(result)
    repeats: dict[str, int] = {}
    _, stuck = _note_repeated_failure(repeats, "browser_click", {"target": "22"}, result)
    assert stuck is False
    assert repeats == {}

    class Page:
        def __init__(self) -> None:
            self.clicks = 0

        def location(self) -> str:
            return "https://shop.test/"

        def read(self):
            return (
                'URL: https://shop.test/\n\nInteractive:\n'
                '[22] button "Legg i handlekurv"\n\nContent:\nHelmelk',
                "",
            )

        def click(self, target: str, role: str = "", ref: str = "") -> None:
            self.clicks += 1
            raise RuntimeError(
                'control [22] is covered by overlay "Cookie" that intercepts pointer '
                "— target [22] still current"
            )

    page = Page()
    browser = Browser("ada", page)
    browser._refs["ada"] = {"22": ("button", "Legg i handlekurv")}
    assistant = Assistant(ner=StubNer())
    assistant.add(browser)
    model = Scripted([
        ModelTurn("", (ToolCall("browser_click", {"target": "22"}),)),
        ModelTurn("", (ToolCall("browser_click", {"target": "22"}),)),
        ModelTurn("", (ToolCall("browser_click", {"target": "22"}),)),
        ModelTurn("The cookie banner is covering the button."),
    ])
    converse(assistant, Task("ada", "t", "add milk"), model, max_steps=6)
    assert page.clicks == 3


def test_form_field_labels_stay_targetable_after_airlock() -> None:
    from robin.airlock import Entity, VocabularyTerm
    from robin.loop import _release_labels, _release_snapshot
    from robin.vault import Vault

    class FormNer(UnavailableNer):
        def available(self) -> bool:
            return True

        def detect(self, text: str):
            found: list[Entity] = []
            email = "ada@example.com"
            phone = "+47 900 00 000"
            if email in text:
                start = text.index(email)
                found.append(Entity(start, start + len(email), "EMAIL"))
            if phone in text:
                start = text.index(phone)
                found.append(Entity(start, start + len(phone), "PHONE"))
            return tuple(found)

    vault = Vault("ada", "t")
    ner = FormNer()
    labels = _release_labels(
        ["ada@example.com", "+47 900 00 000", "Send bestilling"],
        vault,
        (),
        ner,
    )
    assert labels == ["email", "phone", "Send bestilling"]
    assert "[EMAIL" not in "".join(labels)
    assert "[PHONE" not in "".join(labels)

    snapshot = (
        "URL: https://clinic.test/book\nTitle: Book\n\nInteractive:\n"
        '[1] textbox "ada@example.com" (main)\n'
        '[2] textbox "+47 900 00 000" (main)\n'
        '[3] button "Send bestilling" (main, disabled)\n\n'
        "Content:\nBook a visit."
    )
    released = _release_snapshot(snapshot, vault, (VocabularyTerm("clinic"),), ner)
    assert '[1] textbox "email"' in released
    assert '[2] textbox "phone"' in released
    assert '[3] button "Send bestilling" (main, disabled)' in released
    assert "[EMAIL_" not in released
    assert "[PHONE_" not in released


def test_reading_the_page_keeps_tools_for_the_next_step() -> None:
    assistant = Assistant(ner=StubNer())
    assistant.add(Screen(owner="ada", text="Astrid's funeral leads the front page.", password=""))
    model = Scripted(
        [
            ModelTurn("", (ToolCall("screen_read", {}),)),
            ModelTurn("Astrid's funeral leads the front page."),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "what is the news"), model)
    assert reply.text == "Astrid's funeral leads the front page."
    assert "screen_read" in model.seen[1][1]
    assert len(model.seen) == 2


def test_chat_model_posts_openai_tool_messages() -> None:
    seen: list[tuple[str, dict]] = []

    def transport(url: str, body: dict) -> dict:
        seen.append((url, body))
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "lists_show", "arguments": '{"list": "groceries"}'},
                            }
                        ],
                    }
                }
            ]
        }

    model = ChatModel(transport=transport)
    turn = model.complete(
        messages=[{"role": "system", "content": "You are Robin."}, {"role": "user", "content": "list"}],
        tools=[{"name": "lists_show", "description": "List items.", "parameters": {"type": "object"}, "effect": "read"}],
    )
    assert turn.tool_calls[0].name == "lists_show"
    assert turn.tool_calls[0].id == "c1"
    assert seen[0][1]["tools"][0]["function"]["name"] == "lists_show"


def test_model_timeout_is_a_plain_reply(monkeypatch) -> None:
    def transport(url: str, body: dict) -> dict:
        raise TimeoutError()

    model = ChatModel(transport=transport)
    turn = model.complete(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert turn.message == "The model did not answer in time."


def test_model_url_error_is_a_plain_reply() -> None:
    def transport(url: str, body: dict) -> dict:
        raise URLError(TimeoutError())

    model = ChatModel(transport=transport)
    turn = model.complete(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert "time" in turn.message or "not running" in turn.message


def test_model_prompt_and_answer_are_logged(capsys, monkeypatch) -> None:
    monkeypatch.delenv("ROBIN_LOG_MODEL", raising=False)
    assistant = Assistant(ner=StubNer())
    model = Scripted([ModelTurn("hello there")])
    converse(assistant, Task("ada", "t", "hi", allow_cloud=True), model)
    err = capsys.readouterr().err
    assert "--- robin model prompt ---" in err
    assert "Person: hi" in err
    assert "--- robin model answer ---" in err
    assert "hello there" in err


def test_model_logging_can_be_disabled(capsys, monkeypatch) -> None:
    monkeypatch.setenv("ROBIN_LOG_MODEL", "0")
    assistant = Assistant(ner=StubNer())
    model = Scripted([ModelTurn("quiet")])
    converse(assistant, Task("ada", "t", "hi", allow_cloud=True), model)
    err = capsys.readouterr().err
    assert "--- robin model prompt ---" not in err
    assert "--- robin model answer ---" not in err


def test_thread_history_is_sent_redacted(tmp_path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(ner=StubNer(), store=store)
    assistant.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    store.append_turn("ada", "t", "user", "hello Jane Doe")
    store.append_turn("ada", "t", "assistant", "hi")
    model = Scripted([ModelTurn("ok")])
    converse(assistant, Task("ada", "t", "again", allow_cloud=True), model)
    prompt = _user_text(model.seen[0][0])
    assert "Jane Doe" not in prompt
    assert "[PERSON_" in prompt or "hello" in prompt


def test_history_drops_unconfirmed_tool_calls(tmp_path) -> None:
    from robin.loop import _history
    from robin.store import HouseholdStore
    from robin.vault import new_key

    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(ner=StubNer(), store=store)
    store.append_turn("ada", "t", "user", "cheapest fuel nearby?")
    store.append_turn(
        "ada", "t", "confirm", 'Confirm mcp_tavily_tavily_research before Robin does it. {"topic": "cheapest fuel"}'
    )
    store.append_turn("ada", "t", "reply", "Prices vary. I can remember how I did this on example.com for next time.")
    text = _history(
        assistant, Task("ada", "t", "answer in English", allow_cloud=True), assistant.vaults.get("ada", "t"), (), assistant.ner
    )
    assert "mcp_tavily_tavily_research" not in text
    assert "I can remember" not in text
    assert "robin: Prices vary." in text


def test_history_collapses_numbered_booking_menus(tmp_path) -> None:
    from robin.loop import _history
    from robin.store import HouseholdStore
    from robin.vault import new_key

    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(ner=StubNer(), store=store)
    store.append_turn("ada", "t", "user", "book on vethjem.no")
    store.append_turn(
        "ada",
        "t",
        "assistant",
        "Please choose: 1. At the clinic 2. Home visit 3. Video consultation. Which option?",
    )
    store.append_turn("ada", "t", "user", "clinic")
    text = _history(assistant, Task("ada", "t", "clinic again", allow_cloud=True), assistant.vaults.get("ada", "t"), (), assistant.ner)
    assert "1. At the clinic" not in text
    assert "ignore that list" in text
    assert "continue with browser tools" not in text

    store.append_turn(
        "ada",
        "t",
        "assistant",
        "I'm currently unable to access the web to check vg.no. Visit the site directly.",
    )
    text = _history(
        assistant,
        Task("ada", "t", "who are you?", allow_cloud=True),
        assistant.vaults.get("ada", "t"),
        (),
        assistant.ner,
    )
    assert "unable to access" not in text.lower()
    assert "ignore that" in text
    assert "browser_open" not in text

    store.append_turn(
        "ada",
        "t",
        "assistant",
        "Here are the latest news headlines from VG: 1. One 2. Two 3. Three If you'd like more details",
    )
    text = _history(
        assistant,
        Task("ada", "t", "who are you?", allow_cloud=True),
        assistant.vaults.get("ada", "t"),
        (),
        assistant.ner,
    )
    assert "latest news headlines from VG: 1." not in text
    assert "ignore unless the person asks for news" in text

    store.append_turn(
        "ada",
        "t",
        "assistant",
        "Access to the LOT website has been blocked due to security policies, so I'm unable to retrieve flights.",
    )
    text = _history(
        assistant,
        Task("ada", "t", "USE LOT again", allow_cloud=True),
        assistant.vaults.get("ada", "t"),
        (),
        assistant.ner,
    )
    assert "do not retry the same host" not in text
    assert "security policies" in text
    store.append_turn(
        "ada",
        "t",
        "assistant",
        "A captcha or security check is blocking the page. Open the live view.",
    )
    text = _history(
        assistant,
        Task("ada", "t", "USE LOT again", allow_cloud=True),
        assistant.vaults.get("ada", "t"),
        (),
        assistant.ner,
    )
    assert "do not retry the same host" in text
    assert "blocked Robin's automated browser" in text
    assert "wrongly said a website was unreachable" in text  # prior false refusal still collapsed

    from robin.capabilities.browser import Browser
    from robin.capabilities.calendar import Calendar
    from robin.loop import SYSTEM, _system

    assert "latest Person line is the current task" in SYSTEM
    assert "identity questions" in SYSTEM.lower()
    assert "browser tools" in SYSTEM
    assert "own calendar" in SYSTEM or "third-party" in SYSTEM
    assert "confirm before sending" in SYSTEM.lower()
    assert "list, calendar, and memory" in SYSTEM.lower()
    assert "already available" in SYSTEM.lower()
    assert "lack access" in SYSTEM.lower()
    assert "web_search fails" in SYSTEM.lower()
    assert "clinic" not in SYSTEM.lower()
    assert "Send bestilling" not in SYSTEM
    assistant = Assistant(ner=StubNer())
    assistant.add(Browser(owner="ada", page=_LoopPage()))
    assistant.add(Calendar(_NoCal()))
    prompt = _system(assistant, "ada")
    assert "browser" in prompt.lower()
    tools = {tool["name"]: tool["description"] for tool in assistant.tools("ada")}
    assert "browser_open" in tools
    assert "website" in tools["browser_open"].lower() or "booking" in tools["browser_open"].lower()
    assert "clinic" in tools["browser_open"].lower() or "clinic" in tools["browser_click"].lower()
    assert "never invent a numbered menu" in tools["browser_click"].lower()
    assert "browser_fill_profile" in tools
    assert "never appears" in tools["browser_fill_profile"].lower() or "never appear" in tools["browser_fill_profile"].lower()
    assert "browser_fill_profile" in tools["browser_type"].lower()
    assert "disabled" in tools["browser_click"].lower()
    assert "interactive" in tools["browser_click"].lower()
    assert "browser_click" in tools["browser_submit"].lower()
    assert "calendar_add" not in tools
    assert "calendar_* tools are only" in prompt
    assert "jobs_add" in prompt

    from robin.ner import UnavailableNer

    cold = Assistant(ner=UnavailableNer())
    cold_prompt = _system(cold, "ada")
    assert "ner: unavailable" in cold_prompt
    assert "regex and household vocabulary" in cold_prompt
    assert "free-text tool results are [UNRESOLVED]" not in cold_prompt


class _LoopPage:
    def read(self):
        return "URL: https://example.com/\n\nInteractive:\n(none)\n\nContent:\nhi", ""

    def open(self, url: str) -> None:
        return None

    def location(self) -> str:
        return "https://example.com/"

    def needs_login(self) -> bool:
        return False


class _NoCal:
    def connected(self, account_id: str) -> bool:
        return False

    def events(self, account_id: str, start: str = "", end: str = ""):
        return []

    def search(self, account_id: str, query: str):
        return []

    def free_busy(self, account_id: str, start: str, end: str, minutes: int):
        return []

    def add(self, account_id: str, title: str, when: str) -> str:
        return "1"

    def update(self, account_id: str, event_id: str, **fields) -> None:
        return None

    def delete(self, account_id: str, event_id: str) -> None:
        return None


def test_newer_message_drops_stale_in_flight_reply(tmp_path) -> None:
    import threading
    import time

    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(ner=StubNer(), store=store)
    started = threading.Event()
    results: dict[str, object] = {}

    class SlowNews:
        def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
            started.set()
            time.sleep(0.25)
            return ModelTurn("Here are today's headlines from vg.no")

    def run_news() -> None:
        results["news"] = converse(
            assistant,
            Task("ada", "t", "what's the news?", allow_cloud=True),
            SlowNews(),
        )

    def run_who() -> None:
        assert started.wait(timeout=5)
        time.sleep(0.05)
        results["who"] = converse(
            assistant,
            Task("ada", "t", "who are you?", allow_cloud=True),
            Scripted([ModelTurn("I am Robin, your household assistant.")]),
        )

    news_thread = threading.Thread(target=run_news)
    who_thread = threading.Thread(target=run_who)
    news_thread.start()
    who_thread.start()
    news_thread.join(timeout=5)
    who_thread.join(timeout=5)
    assert not news_thread.is_alive() and not who_thread.is_alive()

    news_reply = results["news"]
    who_reply = results["who"]
    assert getattr(news_reply, "text") == "That turn was replaced by a newer message."
    assert "Robin" in getattr(who_reply, "text")
    assert "headlines" not in getattr(who_reply, "text").lower()
    remembered = " ".join(turn["text"] for turn in store.turns("ada", "t"))
    assert "headlines" not in remembered.lower()
    assert "who are you?" in remembered
    assert "Robin" in remembered


def test_person_facing_replies_restore_placeholders() -> None:
    from robin.loop import _for_person, _reply_text
    from robin.policy import Route
    from robin.vault import Vault

    vault = Vault("ada", "home")
    token = vault.token("ORG", "kayak.no")
    reply = _reply_text(f"Sign in to {token} is needed.", vault, (), Route.CLOUD)
    assert "kayak.no" in reply.text
    assert "[ORG_" not in reply.text
    assert _for_person(vault.restore(f"Visit {token} or [ORG_999]")) == "Visit kayak.no or that site"


def test_credential_prompt_to_the_person_is_restored(tmp_path) -> None:
    from robin.airlock import Entity
    from robin.capabilities.browser import Browser, Desk
    from robin.store import HouseholdStore
    from robin.vault import new_key

    class OrgNer(UnavailableNer):
        def available(self) -> bool:
            return True

        def detect(self, text: str):
            host = "accounts.store.example"
            if host not in text:
                return ()
            start = text.index(host)
            return (Entity(start, start + len(host), "ORG"),)

    class LoginGate:
        def __init__(self) -> None:
            self.url = "https://accounts.store.example/login"
            self.form = True

        def open(self, url: str) -> None:
            self.url = url

        def needs_login(self) -> bool:
            return True

        def read(self) -> tuple[str, str]:
            return "Sign in", ""

        def location(self) -> str:
            return self.url

        def clear_gate(self) -> None:
            return None

        def needs_code(self) -> bool:
            return False

    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    page = LoginGate()
    assistant = Assistant(store=store, ner=OrgNer())
    assistant.add(Browser(desk=Desk(lambda url: page), broker=assistant.broker))
    ask = converse(
        assistant,
        Task("ada", "home", "log in to store.example", allow_cloud=True),
        Scripted([ModelTurn("", (ToolCall("browser_open", {"url": "https://store.example/"}),))]),
    )
    assert "accounts.store.example" in ask.text
    assert ask.text.startswith("Sign in to")
    assert "[ORG_" not in ask.text


def test_tool_arguments_are_decoded_leniently() -> None:
    from robin.model import decode_arguments

    assert decode_arguments('{"query": "oslo"}') == ({"query": "oslo"}, "")
    assert decode_arguments('"{\\"query\\": \\"oslo\\"}"') == ({"query": "oslo"}, "")
    assert decode_arguments('```json\n{"query": "oslo"}\n```') == ({"query": "oslo"}, "")
    assert decode_arguments({"query": "oslo"}) == ({"query": "oslo"}, "")
    assert decode_arguments("") == ({}, "")
    arguments, error = decode_arguments("query=oslo")
    assert arguments == {} and "not a JSON object" in error


def test_a_call_missing_required_arguments_is_sent_back_to_the_model() -> None:
    lists = Lists(members={"ada"}, shared=[{"item": "milk", "list": "groceries", "loyalty": ""}], private={})
    assistant = Assistant(ner=StubNer())
    assistant.add(lists)
    model = Scripted(
        [
            ModelTurn("", (ToolCall("lists_show", {}),)),
            ModelTurn("", (ToolCall("lists_show", {}, error="arguments were not a JSON object: x"),)),
            ModelTurn("", (ToolCall("lists_show", {"list": "groceries"}),)),
            ModelTurn("done"),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "what should I buy"), model)
    assert reply.text == "done"
    tool_messages = [message for message in model.seen[3][0] if message.get("role") == "tool"]
    assert "missing required argument(s) list" in tool_messages[0]["content"]
    assert "not a JSON object" in tool_messages[1]["content"]
    assert "Not run" not in tool_messages[2]["content"]


def _tool_pairs_ok(messages: list[dict]) -> bool:
    for index, message in enumerate(messages):
        ids = [call["id"] for call in message.get("tool_calls") or []]
        if not ids:
            continue
        following = []
        for later in messages[index + 1 :]:
            if later.get("role") != "tool":
                break
            following.append(later["tool_call_id"])
        if following != ids:
            return False
    return all(
        message.get("role") != "tool" or index > 0 and messages[index - 1].get("role") in ("assistant", "tool")
        for index, message in enumerate(messages)
    )


def test_household_context_reaches_the_model_before_tools() -> None:
    from datetime import datetime

    from robin.capabilities.jobs import Jobs
    from robin.capabilities.memory import Memory
    from robin.loop import _system

    lists = Lists(members={"ada"}, shared=[{"item": "oat milk", "list": "groceries", "loyalty": ""}], private={})
    jobs = Jobs(clock=lambda: datetime(2026, 9, 23, 18, 0))
    memory = Memory(facts={"ada": []})
    memory.invoke("ada", "lesson_save", {"text": "The household is vegetarian", "kind": "fact"})
    memory.invoke("ada", "lesson_save", {"text": "Prefer short replies", "kind": "preference"})
    for row in memory._facts["ada"]:
        if "short" in row["text"]:
            row["hits"] = 99
    assistant = Assistant(ner=StubNer())
    assistant.add(lists)
    assistant.add(jobs)
    assistant.add(memory)
    converse(
        assistant,
        Task("ada", "home", "remind me to take out the trash every evening"),
        Scripted(
            [
                ModelTurn(
                    "",
                    (
                        ToolCall(
                            "jobs_add",
                            {"instruction": "take out the trash", "hour": 18, "minute": 0, "days": "every day"},
                        ),
                    ),
                ),
                ModelTurn("Saved."),
            ]
        ),
    )
    model = Scripted([ModelTurn("Pasta works.")])
    converse(assistant, Task("ada", "home", "what can we cook from the fridge for vegetarians"), model)
    prompt = model.seen[0][0][0]["content"]
    assert "On this account (use these before guessing)" in prompt
    assert "oat milk" in prompt
    assert "take out the trash" in prompt
    assert "The household is vegetarian" in prompt
    selected = memory.select("ada", "what can we cook from the fridge for vegetarians")
    assert selected[0]["text"] == "The household is vegetarian"
    stored = jobs._jobs["ada"][0]
    assert stored["conversation_id"] == "home"
    due = jobs.due(datetime(2026, 9, 24, 18, 0))
    assert due and due[0].conversation_id == "home"
    job_prompt = _system(assistant, "ada", "take out the trash", vault=assistant.vaults.get("ada", "home"))
    assert "oat milk" in job_prompt


def test_confirm_does_not_abort_readonly_calls_in_the_same_batch() -> None:
    from robin.loop import resume

    screen = Screen(owner="ada", text="desk", password="")
    lists = Lists(members={"ada"}, shared=[{"item": "milk", "list": "groceries", "loyalty": ""}], private={})
    assistant = Assistant(ner=StubNer())
    assistant.add(screen)
    assistant.add(lists)
    model = Scripted(
        [
            ModelTurn(
                "",
                (
                    ToolCall("lists_show", {"list": "groceries"}, id="a"),
                    ToolCall("screen_submit", {}, id="b"),
                    ToolCall("lists_show", {"list": "groceries"}, id="c"),
                ),
            ),
            ModelTurn("Done."),
        ]
    )
    held = converse(assistant, Task("ada", "t", "check dinner and send the invite"), model)
    assert held.status == "confirm"
    assert held.tool == "screen_submit"
    assert held.task_text == "check dinner and send the invite"
    pending = assistant._pending[("ada", "t")]
    assert pending["text"] == "check dinner and send the invite"
    done_ids = {item["id"] for item in pending["done_calls"]}
    assert done_ids == {"a", "c"}
    assert screen.submitted is False
    reply = resume(assistant, "ada", "t", model)
    assert reply.text == "Done."
    assert screen.submitted is True


def test_confirming_one_of_several_calls_runs_the_rest() -> None:
    from robin.loop import resume

    screen = Screen(owner="ada", text="desk", password="")
    lists = Lists(members={"ada"}, shared=[{"item": "milk", "list": "groceries", "loyalty": ""}], private={})
    assistant = Assistant(ner=StubNer())
    assistant.add(screen)
    assistant.add(lists)
    model = Scripted(
        [
            ModelTurn(
                "",
                (
                    ToolCall("lists_show", {"list": "groceries"}, id="a"),
                    ToolCall("screen_submit", {}, id="b"),
                    ToolCall("lists_show", {"list": "groceries"}, id="c"),
                ),
            ),
            ModelTurn("Done."),
        ]
    )
    held = converse(assistant, Task("ada", "t", "check and send"), model)
    assert held.status == "confirm"
    reply = resume(assistant, "ada", "t", model)
    assert reply.text == "Done."
    assert screen.submitted is True
    final = model.seen[-1][0]
    assert _tool_pairs_ok(final)
    results = {message["tool_call_id"]: message["content"] for message in final if message.get("role") == "tool"}
    assert set(results) == {"a", "b", "c"}
    assert "milk" in results["a"].casefold() and "milk" in results["c"].casefold()


def test_model_requests_always_pair_tool_calls() -> None:
    from robin.loop import _paired

    messages = [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "orphan", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "user", "content": "nudge"},
    ]
    paired = _paired(messages)
    assert [m.get("tool_call_id") for m in paired if m.get("role") == "tool"] == ["a", "b"]
    assert paired[-1]["content"] == "nudge"
    assert paired[3]["content"] == "Not run."


def test_model_http_errors_are_named() -> None:
    import io
    from urllib.error import HTTPError

    def refuse(code: int, body: str):
        def transport(url, body_):
            raise HTTPError(url, code, "err", {"Retry-After": "0"}, io.BytesIO(body.encode()))

        return transport

    turn = ChatModel(transport=refuse(400, '{"error":{"message":"tool_call_ids did not have response messages"}}')).complete(messages=[], tools=[])
    assert "HTTP 400" in turn.message and "not running" not in turn.message
    turn = ChatModel(transport=refuse(400, '{"error":{"code":"context_length_exceeded"}}')).complete(messages=[], tools=[])
    assert "too long" in turn.message
    turn = ChatModel(transport=refuse(429, "slow down")).complete(messages=[], tools=[])
    assert "rate limited" in turn.message

    attempts = []

    def flaky(url, body):
        attempts.append(1)
        if len(attempts) == 1:
            raise HTTPError(url, 503, "err", {"Retry-After": "0"}, io.BytesIO(b""))
        return {"choices": [{"message": {"content": "hi"}}]}

    assert ChatModel(transport=flaky).complete(messages=[], tools=[]).message == "hi"


def test_calendar_tools_hidden_until_connected() -> None:
    from robin.capabilities.calendar import Calendar

    class _Cal(_NoCal):
        linked = False

        def connected(self, account_id: str) -> bool:
            return self.linked

    cal = _Cal()
    assistant = Assistant(ner=StubNer())
    assistant.add(Calendar(cal))
    assert not [tool for tool in assistant.tools("ada") if tool["name"].startswith("calendar_")]
    cal.linked = True
    assert "calendar_add" in {tool["name"] for tool in assistant.tools("ada")}


class _Trip(Capability):
    id = "transit"
    tools = [
        Tool(
            name="transit_trip",
            description="Plan a public transit trip.",
            parameters={
                "type": "object",
                "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                "required": ["from", "to"],
            },
            effect=Effect.READ,
        )
    ]
    fields = []

    def invoke(self, account_id: str, tool_name: str, arguments: dict) -> str:
        return f"Next bus for Jane Doe in 5 minutes from {arguments.get('from')}"


class _Inbox(Capability):
    id = "post"
    tools = [
        Tool(
            name="mail_list",
            description="List recent messages.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        )
    ]
    fields = []

    def invoke(self, account_id: str, tool_name: str, arguments: dict) -> str:
        return "Inbox: one note about soccer practice from jane@example.com"


def test_multi_step_request_keeps_going_after_first_prose() -> None:
    lists = Lists(members={"ada"}, shared=[], private={})
    assistant = Assistant(ner=StubNer())
    assistant.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    assistant.add(_Trip())
    assistant.add(lists)
    assistant.add(_Inbox())
    model = Scripted(
        [
            ModelTurn("", (ToolCall("transit_trip", {"from": "home", "to": "work"}),)),
            ModelTurn("The next bus is in 5 minutes."),
            ModelTurn("", (ToolCall("lists_add", {"list": "groceries", "item": "milk"}),)),
            ModelTurn("", (ToolCall("mail_list", {}),)),
            ModelTurn("Bus in 5 minutes, milk is on the list, and the inbox has one note."),
        ]
    )
    reply = converse(
        assistant,
        Task("ada", "t", "check the bus, add milk to the list, and look at mail"),
        model,
    )
    assert "milk" in reply.text.lower() or "inbox" in reply.text.lower()
    assert len(model.seen) >= 4
    nudged = any(
        "not finished" in str(message.get("content") or "").lower()
        for messages, _tools in model.seen
        for message in messages
        if message.get("role") == "user"
    )
    assert nudged
    invoked = [entry["tool"] for entry in assistant.activity.read("ada")]
    assert "transit_trip" in invoked
    assert "lists_add" in invoked
    assert "mail_list" in invoked


def test_step_limit_reports_unfinished_multi_step_work() -> None:
    lists = Lists(members={"ada"}, shared=[], private={})
    assistant = Assistant(ner=StubNer())
    assistant.add(_Trip())
    assistant.add(lists)
    assistant.add(_Inbox())
    model = Scripted(
        [
            ModelTurn("", (ToolCall("transit_trip", {"from": "home", "to": "work"}),)),
            ModelTurn("Everything is done for the house."),
        ]
    )
    reply = converse(
        assistant,
        Task("ada", "t", "check the bus, add milk to the list, and look at mail"),
        model,
        max_steps=1,
    )
    assert "unfinished" in reply.text.lower()
    assert "list" in reply.text.lower() or "mail" in reply.text.lower()
    assert len(model.seen) == 1
    assert "everything is done" not in reply.text.lower()


def test_follow_up_sees_prior_tool_trace_without_secrets(tmp_path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(ner=StubNer(), store=store)
    assistant.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    assistant.add(_Trip())
    assistant.add(Lists(members={"ada"}, shared=[], private={}))
    first = Scripted(
        [
            ModelTurn(
                "",
                (
                    ToolCall("transit_trip", {"from": "home", "to": "work"}, id="bus"),
                    ToolCall("lists_add", {"list": "groceries", "item": SECRET}, id="list"),
                ),
            ),
            ModelTurn("The bus is soon and the list is updated."),
        ]
    )
    converse(
        assistant,
        Task("ada", "t", "check the bus and add milk to the list for Jane Doe", allow_cloud=True),
        first,
    )
    second = Scripted(
        [
            ModelTurn("I will do the same for the other child."),
            ModelTurn(
                "",
                (
                    ToolCall("transit_trip", {"from": "home", "to": "work"}, id="bus2"),
                    ToolCall("lists_add", {"list": "groceries", "item": "milk"}, id="list2"),
                ),
            ),
            ModelTurn("Done for the other child."),
        ]
    )
    converse(
        assistant,
        Task("ada", "t", "do the same for the other kid", allow_cloud=True),
        second,
    )
    prompt = _user_text(second.seen[0][0])
    assert "Prior tools:" in prompt
    assert "transit_trip" in prompt
    assert "lists_add" in prompt
    assert "Jane Doe" not in prompt
    assert SECRET not in prompt
    assert "Open task:" in prompt
    assert "sk-" not in prompt


def test_tool_and_history_text_stay_usable_when_ner_is_cold(tmp_path) -> None:
    from robin.airlock import UNRESOLVED
    from robin.loop import _release, _release_snapshot
    from robin.vault import Vault

    vault = Vault("ada", "t")
    vocabulary = (VocabularyTerm("Jane Doe"),)
    ner = UnavailableNer()
    text = "Jane Doe wrote jane@example.com about soccer practice. Next bus is at 08:10."
    released = _release(text, vault, vocabulary, ner, free_text=True)
    assert released != UNRESOLVED
    assert "Jane Doe" not in released
    assert "jane@example.com" not in released
    assert "soccer practice" in released
    assert "08:10" in released
    assert vault.token("PERSON", "Jane Doe") in released

    snapshot = (
        "URL: https://mail.test/\nTitle: Inbox\n\nInteractive:\n"
        '[1] button "Save" (main)\n\n'
        "Content:\nJane Doe wrote about soccer.\nNo matching listings"
    )
    page = _release_snapshot(snapshot, vault, vocabulary, ner)
    assert "withheld:" not in page
    assert "soccer" in page
    assert "No matching listings" in page
    assert "Jane Doe" not in page
    assert '[1] button "Save"' in page

    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(ner=UnavailableNer(), store=store)
    assistant.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    assistant.add(_Inbox())
    store.append_turn("ada", "t", "user", "hello Jane Doe about soccer")
    store.append_turn("ada", "t", "assistant", "Jane Doe has soccer at 08:10")
    model = Scripted(
        [
            ModelTurn("", (ToolCall("mail_list", {}),)),
            ModelTurn("There is a soccer note."),
        ]
    )
    converse(assistant, Task("ada", "t", "look at mail", allow_cloud=True), model)
    history = "\n".join(
        str(message.get("content") or "")
        for messages, _tools in model.seen
        for message in messages
        if message.get("role") in {"user", "tool"}
    )
    assert "soccer" in history
    assert "Jane Doe" not in history
    assert "jane@example.com" not in history
    assert UNRESOLVED not in history
    tool_text = "\n".join(
        str(message.get("content") or "")
        for messages, _tools in model.seen
        for message in messages
        if message.get("role") == "tool"
    )
    assert "soccer" in tool_text
    assert UNRESOLVED not in tool_text
