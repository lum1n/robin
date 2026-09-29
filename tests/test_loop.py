from urllib.error import URLError
from urllib.request import Request

from robin.airlock import VocabularyTerm
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
    local_model = Scripted([ModelTurn("hello [PERSON_1]")])
    local_reply = converse(local, Task("ada", "t", "hello Jane Doe", allow_cloud=False), local_model)
    assert local_reply.route is Route.LOCAL
    prompt = _user_text(local_model.seen[0][0])
    assert "Jane Doe" not in prompt
    assert "[PERSON_1]" in prompt
    assert local_reply.text == "hello Jane Doe"

    cloud = Assistant(ner=StubNer())
    cloud.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    cloud_model = Scripted([ModelTurn("hello [PERSON_1]")])
    cloud_reply = converse(cloud, Task("ada", "t", "hello Jane Doe", allow_cloud=True), cloud_model)
    assert cloud_reply.route is Route.CLOUD
    prompt = _user_text(cloud_model.seen[0][0])
    assert "Jane Doe" not in prompt
    assert "[PERSON_1]" in prompt
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
    assert "[PERSON_1]" in prompt or "hello" in prompt
