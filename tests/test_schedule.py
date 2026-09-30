from robin.capabilities.notify import Notify
from robin.capabilities.screen import Screen
from robin.http import Service, dispatch
from robin.model import ModelTurn, ToolCall
from robin.schedule import tick
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


class Scripted:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.calls = 0

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.calls += 1
        return self.turns.pop(0)


def test_a_check_stays_off_until_that_account_turns_it_on(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    screen = Screen(owner="ada", text="desk", password="")
    assistant = Assistant(store=HouseholdStore(path, key))
    assistant.add(screen)
    notify = Notify(store=assistant.store)
    assistant.add(notify)
    model = Scripted([ModelTurn("", (ToolCall("screen_submit", {}),))])
    assert tick(assistant, model) == []
    assert model.calls == 0

    service = Service(assistant, model)
    service.auth.register("ada", "ada-session-password")
    service.auth.register("bea", "bea-session-password")
    ada = {"authorization": f"Bearer {service.auth.login('ada', 'ada-session-password')}"}
    bea = {"authorization": f"Bearer {service.auth.login('bea', 'bea-session-password')}"}
    stolen, _stolen_body = dispatch(
        service,
        "POST",
        "/v1/schedule",
        body={"account_id": "ada", "enabled": True},
        headers=bea,
    )
    assert stolen == 401
    quiet, quiet_body = dispatch(service, "GET", "/v1/schedule", query={"account_id": "ada"}, headers=ada)
    assert quiet == 200
    assert quiet_body == {"enabled": False}

    status, body = dispatch(service, "POST", "/v1/schedule", body={"account_id": "ada", "enabled": True}, headers=ada)
    assert status == 200
    assert body == {"enabled": True}
    assistant.store.close()

    revived = Assistant(store=HouseholdStore(path, key))
    revived.add(screen)
    revived_notify = Notify(store=revived.store)
    revived.add(revived_notify)
    assert revived.schedule_enabled("ada") is True
    assert revived.schedule_enabled("bea") is False
    replies = tick(revived, model)
    assert model.calls == 1
    assert replies[0].status == "confirm"
    assert replies[0].tool == "screen_submit"
    assert screen.submitted is False
    pending = revived.take_pending("ada", "schedule")
    assert pending is not None
    assert pending["tool"] == "screen_submit"
    attention = revived_notify.pending("ada")
    assert len(attention) == 1
    assert attention[0]["kind"] == "confirm"
