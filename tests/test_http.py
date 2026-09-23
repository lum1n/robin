from robin.airlock import VocabularyTerm
from robin.capabilities.screen import Screen
from robin.http import Service, dispatch
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

FODSELSNUMMER = "01010000110"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


class Scripted:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.seen: list[str] = []

    def complete(self, *, system: str, user: str, tools: list[dict]) -> ModelTurn:
        self.seen.append(user)
        return self.turns.pop(0)


class StubNer(UnavailableNer):
    def available(self) -> bool:
        return True


def _service(tmp_path, model, ner=None) -> tuple[Service, Screen, dict[str, str], dict[str, str]]:
    screen = Screen(owner="ada", text="desk", password="")
    assistant = Assistant(ner=ner, store=HouseholdStore(tmp_path / "house.sqlite", new_key()))
    assistant.add(screen)
    assistant.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    service = Service(assistant, model)
    service.auth.register("ada", "ada-session-password")
    service.auth.register("bea", "bea-session-password")
    ada = {"authorization": f"Bearer {service.auth.login('ada', 'ada-session-password')}"}
    bea = {"authorization": f"Bearer {service.auth.login('bea', 'bea-session-password')}"}
    return service, screen, ada, bea


def test_a_message_can_stop_for_confirmation_and_a_second_call_runs_it(tmp_path) -> None:
    model = Scripted([ModelTurn("", (ToolCall("submit", {}),))])
    service, screen, ada, _bea = _service(tmp_path, model)
    held = dispatch(
        service,
        "POST",
        "/v1/messages",
        body={"account_id": "ada", "conversation_id": "kitchen", "text": "send it"},
        headers=ada,
    )
    assert held[0] == 200
    assert held[1]["status"] == "confirm"
    assert held[1]["tool"] == "submit"
    assert screen.submitted is False

    done = dispatch(
        service,
        "POST",
        "/v1/messages",
        body={"account_id": "ada", "conversation_id": "kitchen", "confirm": True},
        headers=ada,
    )
    assert done[0] == 200
    assert done[1]["status"] == "reply"
    assert screen.submitted is True
    again = dispatch(
        service,
        "POST",
        "/v1/messages",
        body={"account_id": "ada", "conversation_id": "kitchen", "confirm": True},
        headers=ada,
    )
    assert again[0] == 409


def test_threads_and_activity_stay_on_the_account_that_owns_them(tmp_path) -> None:
    model = Scripted([ModelTurn("hello Jane Doe")])
    service, _screen, ada, bea = _service(tmp_path, model)
    dispatch(
        service,
        "POST",
        "/v1/messages",
        body={"account_id": "ada", "conversation_id": "kitchen", "text": f"id {FODSELSNUMMER} secret {SECRET}"},
        headers=ada,
    )
    ada_threads = dispatch(service, "GET", "/v1/threads", query={"account_id": "ada"}, headers=ada)
    bea_threads = dispatch(service, "GET", "/v1/threads", query={"account_id": "bea"}, headers=bea)
    ada_turns = dispatch(service, "GET", "/v1/threads/kitchen", query={"account_id": "ada"}, headers=ada)
    bea_turns = dispatch(service, "GET", "/v1/threads/kitchen", query={"account_id": "bea"}, headers=bea)
    stolen = dispatch(service, "GET", "/v1/threads", query={"account_id": "bea"}, headers=ada)
    assert ada_threads[1]["threads"] == ["kitchen"]
    assert bea_threads[1]["threads"] == []
    assert any(turn["text"] == "hello Jane Doe" for turn in ada_turns[1]["turns"])
    assert bea_turns[1]["turns"] == []
    assert FODSELSNUMMER not in model.seen[0]
    assert SECRET not in model.seen[0]
    activity = dispatch(service, "GET", "/v1/activity", query={"account_id": "bea"}, headers=bea)
    assert activity[1]["entries"] == []
    assert stolen[0] == 401


def test_cloud_opt_in_sends_placeholders_and_the_reply_restores_them(tmp_path) -> None:
    model = Scripted([ModelTurn("hello [PERSON_1]")])
    service, _screen, ada, _bea = _service(tmp_path, model, ner=StubNer())
    status, payload = dispatch(
        service,
        "POST",
        "/v1/messages",
        body={
            "account_id": "ada",
            "conversation_id": "kitchen",
            "text": "hello Jane Doe",
            "allow_cloud": True,
        },
        headers=ada,
    )
    assert status == 200
    assert payload["route"] == "cloud"
    assert payload["text"] == "hello Jane Doe"
    assert "Jane Doe" not in model.seen[0]
    assert "[PERSON_1]" in model.seen[0]


def test_a_request_without_an_account_is_rejected(tmp_path) -> None:
    service, _screen, _ada, _bea = _service(tmp_path, Scripted([ModelTurn("nope")]))
    status, payload = dispatch(service, "POST", "/v1/messages", body={"text": "hello"})
    assert status == 400
    assert "account_id" in payload["error"]
