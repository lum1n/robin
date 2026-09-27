from urllib.error import URLError
from urllib.request import Request

from robin.airlock import VocabularyTerm
from robin.capabilities.groceries import Groceries
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
        self.seen: list[tuple[str, list[str]]] = []

    def complete(self, *, system: str, user: str, tools: list[dict]) -> ModelTurn:
        self.seen.append((user, [tool["name"] for tool in tools]))
        return self.turns.pop(0)


class StubNer(UnavailableNer):
    def available(self) -> bool:
        return True


def test_local_model_sees_the_name_and_a_cloud_model_sees_the_placeholder() -> None:
    local = Assistant(ner=StubNer())
    local.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    local_model = Scripted([ModelTurn("hello [PERSON_1]")])
    local_reply = converse(local, Task("ada", "t", "hello Jane Doe", allow_cloud=False), local_model)
    assert local_reply.route is Route.LOCAL
    assert "Jane Doe" not in local_model.seen[0][0]
    assert "[PERSON_1]" in local_model.seen[0][0]
    assert local_reply.text == "hello Jane Doe"

    cloud = Assistant(ner=StubNer())
    cloud.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    cloud_model = Scripted([ModelTurn("hello [PERSON_1]")])
    cloud_reply = converse(cloud, Task("ada", "t", "hello Jane Doe", allow_cloud=True), cloud_model)
    assert cloud_reply.route is Route.CLOUD
    assert "Jane Doe" not in cloud_model.seen[0][0]
    assert "[PERSON_1]" in cloud_model.seen[0][0]
    assert cloud_reply.text == "hello Jane Doe"


def test_a_secret_in_the_reply_is_dropped_and_never_sent() -> None:
    screen = Screen(owner="ada", text=f"id {FODSELSNUMMER}", password=SECRET)
    assistant = Assistant()
    assistant.add(screen)
    assistant.broker.put("ada", "exe", API_TOKEN)
    model = Scripted([ModelTurn(f"your id is {FODSELSNUMMER}")])
    reply = converse(assistant, Task("ada", "t", "what is on screen", allow_cloud=True), model)
    assert reply.route is Route.LOCAL
    prompt = model.seen[0][0]
    assert FODSELSNUMMER not in prompt
    assert SECRET not in prompt
    assert API_TOKEN not in prompt
    assert FODSELSNUMMER not in reply.text
    assert SECRET not in reply.text


def test_a_read_tool_runs_and_an_external_tool_stops() -> None:
    screen = Screen(owner="ada", text="desk", password="")
    groceries = Groceries(members={"ada"}, shared=[{"item": "milk", "loyalty": ""}], private={})
    assistant = Assistant()
    assistant.add(screen)
    assistant.add(groceries)
    model = Scripted(
        [
            ModelTurn("", (ToolCall("list_items", {}),)),
            ModelTurn("You have milk."),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "what should I buy"), model)
    assert reply.status == "reply"
    assert reply.text == "You have milk."
    assert "Tool list_items returned:" in model.seen[1][0]

    external = Scripted([ModelTurn("", (ToolCall("submit", {}),))])
    held = converse(assistant, Task("ada", "t2", "send it"), external)
    assert held.status == "confirm"
    assert held.tool == "submit"
    assert screen.submitted is False
    assert len(external.seen) == 1


def test_a_hidden_tool_is_not_run() -> None:
    screen = Screen(owner="ada", text="desk", password="")
    assistant = Assistant()
    assistant.add(screen)
    model = Scripted([ModelTurn("", (ToolCall("read_screen", {}),))])
    reply = converse(assistant, Task("bea", "t", "look"), model)
    assert reply.text == "That action is not available."
    assert screen.clicked is False
    assert "read_screen" not in model.seen[0][1]


def test_the_loop_stops_after_the_step_limit() -> None:
    groceries = Groceries(members={"ada"}, shared=[{"item": "milk", "loyalty": ""}], private={})
    assistant = Assistant()
    assistant.add(groceries)
    model = Scripted(
        [ModelTurn("", (ToolCall("list_items", {}),)) for _ in range(4)] + [ModelTurn("Milk is on the list.")]
    )
    reply = converse(assistant, Task("ada", "t", "again"), model, max_steps=4)
    assert reply.text == "Milk is on the list."
    assert len(model.seen) == 5
    assert model.seen[-1][1] == []


def test_reading_the_page_keeps_tools_for_the_next_step() -> None:
    assistant = Assistant()
    assistant.add(Screen(owner="ada", text="Astrid's funeral leads the front page.", password=""))
    model = Scripted(
        [
            ModelTurn("", (ToolCall("read_screen", {}),)),
            ModelTurn("Astrid's funeral leads the front page."),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "what is the news"), model)
    assert reply.text == "Astrid's funeral leads the front page."
    assert "read_screen" in model.seen[1][1]
    assert "Action log:" in model.seen[1][0]
    assert len(model.seen) == 2


def test_a_page_prepare_still_allows_click() -> None:
    from robin.capabilities.browser import Browser, Desk
    from robin.ner import UnavailableNer

    class ReadyNer(UnavailableNer):
        def available(self) -> bool:
            return True

    class News:
        def __init__(self) -> None:
            self.clicked = ""
            self.url = "https://news.test/"

        def open(self, url: str) -> None:
            self.url = url

        def read(self) -> tuple[str, str]:
            return (
                'URL: https://news.test/\nTitle: News\n\nInteractive:\n[1] link "Storm"\n\nContent:\nStorm hits the coast',
                "",
            )

        def click(self, target: str, role: str = "") -> None:
            self.clicked = target

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

        def location(self) -> str:
            return self.url

        def sign_in(self, user: str, password: str) -> None:
            return None

        def needs_code(self) -> bool:
            return False

        def submit_code(self, code: str) -> None:
            return None

    page = News()
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Browser(desk=Desk(lambda url: page)))
    model = Scripted(
        [
            ModelTurn("", (ToolCall("click", {"target": "1"}),)),
            ModelTurn("Opened the storm story."),
        ]
    )
    reply = converse(assistant, Task("ada", "t", "open the storm link on news.test"), model)
    assert page.clicked == "Storm"
    assert reply.text == "Opened the storm story."
    assert "click" in model.seen[0][1]
    assert "Current page:" in model.seen[0][0]


def test_resume_after_confirm_continues_with_the_model() -> None:
    from robin.loop import resume

    screen = Screen(owner="ada", text="form", password="")
    assistant = Assistant()
    assistant.add(screen)
    held_model = Scripted([ModelTurn("", (ToolCall("submit", {}),))])
    held = converse(assistant, Task("ada", "t", "send the form"), held_model)
    assert held.status == "confirm"
    assistant.set_pending(
        "ada",
        "t",
        held.tool or "submit",
        held.arguments or {},
        held.route.value,
        text=held.task_text or "send the form",
    )
    follow = Scripted([ModelTurn("The form was sent.")])
    reply = resume(assistant, "ada", "t", follow)
    assert screen.submitted is True
    assert reply.status == "reply"
    assert reply.text == "The form was sent."
    assert "submit" in follow.seen[0][0] or "Action log:" in follow.seen[0][0]


def test_chat_model_posts_to_the_local_completions_url() -> None:
    seen: list[tuple[str, dict]] = []

    def transport(url: str, body: dict) -> dict:
        seen.append((url, body))
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "list_items", "arguments": "{\"aisle\": \"dairy\"}"}}
                        ],
                    }
                }
            ]
        }

    model = ChatModel("http://127.0.0.1:8080", transport=transport)
    turn = model.complete(
        system="stay local",
        user="buy milk",
        tools=[{"name": "list_items", "description": "List items.", "parameters": {"type": "object"}, "effect": "read"}],
    )
    assert seen[0][0] == "http://127.0.0.1:8080/v1/chat/completions"
    assert seen[0][1]["messages"][1]["content"] == "buy milk"
    assert seen[0][1]["tools"][0]["function"]["name"] == "list_items"
    assert "effect" not in seen[0][1]["tools"][0]["function"]
    assert turn.tool_calls[0].arguments == {"aisle": "dairy"}


def test_a_quiet_local_model_becomes_a_reply() -> None:
    def down(url: str, body: dict) -> dict:
        raise URLError("connection refused")

    def slow(url: str, body: dict) -> dict:
        raise TimeoutError("timed out")

    missed = ChatModel("http://127.0.0.1:8080", transport=down)
    assert missed.complete(system="", user="hello", tools=[]).message == "The model is not running."
    late = ChatModel("http://127.0.0.1:8080", transport=slow)
    assert late.complete(system="", user="hello", tools=[]).message == "The model did not answer in time."


def test_a_remote_model_gets_a_bearer_token_outside_the_prompt(monkeypatch) -> None:
    captured: dict[str, Request] = {}

    class Response:
        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"ok"}}]}'

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_open(request: Request, timeout: int = 0) -> Response:
        captured["request"] = request
        return Response()

    monkeypatch.setattr("robin.model.urlopen", fake_open)
    reply = ChatModel("https://api.openai.com", model="gpt-4o-mini", api_key="sk-testkey").complete(
        system="stay",
        user="hello",
        tools=[],
    )
    request = captured["request"]
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer sk-testkey"
    raw = request.data.decode()
    assert "sk-testkey" not in raw
    assert '"model": "gpt-4o-mini"' in raw
    assert reply.message == "ok"


def test_hello_does_not_send_the_whole_account_context() -> None:
    screen = Screen(owner="ada", text="z" * 80_000, password="")
    assistant = Assistant()
    assistant.add(screen)
    model = Scripted([ModelTurn("Hi.")])
    reply = converse(assistant, Task("ada", "home", "hello"), model)
    assert reply.text == "Hi."
    sent = model.seen[0][0]
    assert "hello" in sent
    assert len(sent) <= 6000
    assert "z" * 4000 not in sent


def test_the_model_sees_the_recent_conversation(tmp_path) -> None:
    assistant = Assistant(store=HouseholdStore(tmp_path / "house.sqlite", new_key()))
    model = Scripted([ModelTurn("Ada."), ModelTurn("Your dog is Ada."), ModelTurn("I only know this thread.")])
    converse(assistant, Task("ada", "home", "my dog is named Ada"), model)
    converse(assistant, Task("ada", "other", "the codeword is plum"), model)
    converse(assistant, Task("ada", "home", "what is my dog's name?"), model)
    sent = model.seen[2][0]
    assert "what is my dog's name?" in sent
    assert "my dog is named Ada" not in sent
    assert "plum" not in sent
    assert "[UNRESOLVED]" in sent
