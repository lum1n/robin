from robin.airlock import VocabularyTerm
from robin.capabilities.groceries import Groceries
from robin.capabilities.screen import Screen
from robin.model import ChatModel, ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Route, Task
from robin.loop import converse
from robin.session import Assistant

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
    assert "Jane Doe" in local_model.seen[0][0]
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
    model = Scripted([ModelTurn("", (ToolCall("list_items", {}),)) for _ in range(6)])
    reply = converse(assistant, Task("ada", "t", "again"), model, max_steps=4)
    assert reply.text == "Stopped after the step limit."
    assert len(model.seen) == 4


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
