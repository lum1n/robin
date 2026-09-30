"""Attention notifications for the Robin app."""

from __future__ import annotations

from robin.capabilities.notify import Notify
from robin.capabilities.screen import Screen
from robin.http import Service, dispatch
from robin.loop import Reply
from robin.model import ModelTurn, ToolCall
from robin.policy import Route
from robin.schedule import _notify_attention, tick
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


class Scripted:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        return self.turns.pop(0)


def test_notify_enqueue_list_and_ack(tmp_path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    notify = Notify(store=store)
    record = notify.enqueue("ada", "Dinner is ready", kind="notify", conversation_id="home")
    assert record["id"]
    assert record["text"] == "Dinner is ready"
    pending = notify.pending("ada")
    assert len(pending) == 1
    assert pending[0]["id"] == record["id"]
    loaded = store.list_notifications("ada")
    assert loaded[0]["text"] == "Dinner is ready"
    assert notify.ack("ada", [record["id"]]) == 1
    assert notify.pending("ada") == []
    assert store.list_notifications("ada") == []


def test_http_notifications_round_trip(tmp_path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    assistant = Assistant(store=store)
    notify = Notify(store=store)
    assistant.add(notify)
    notify.enqueue("ada", "Confirm send?", kind="confirm", conversation_id="schedule")
    service = Service(assistant, Scripted([]))
    service.auth.register("ada", "pw")
    ada = {"authorization": f"Bearer {service.auth.login('ada', 'pw')}"}
    status, body = dispatch(service, "GET", "/v1/notifications", query={"account_id": "ada"}, headers=ada)
    assert status == 200
    assert len(body["notifications"]) == 1
    item_id = body["notifications"][0]["id"]
    denied, _ = dispatch(service, "GET", "/v1/notifications", query={"account_id": "ada"}, headers={})
    assert denied == 401
    acked, ack_body = dispatch(
        service,
        "POST",
        "/v1/notifications",
        body={"account_id": "ada", "ack": [item_id]},
        headers=ada,
    )
    assert acked == 200
    assert ack_body["acked"] == 1
    empty, empty_body = dispatch(service, "GET", "/v1/notifications", query={"account_id": "ada"}, headers=ada)
    assert empty == 200
    assert empty_body["notifications"] == []


def test_schedule_confirm_enqueues_notification(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    assistant = Assistant(store=store)
    assistant.add(Screen(owner="ada", text="desk", password=""))
    notify = Notify(store=store)
    assistant.add(notify)
    assistant.set_schedule("ada", True)
    model = Scripted([ModelTurn("", (ToolCall("screen_submit", {}),))])
    replies = tick(assistant, model)
    assert replies[0].status == "confirm"
    pending = notify.pending("ada")
    assert len(pending) == 1
    assert pending[0]["kind"] == "confirm"
    assert pending[0]["conversation_id"] == "schedule"
    assert "screen_submit" in pending[0]["text"]


def test_notify_for_reply_ignores_plain_replies() -> None:
    assistant = Assistant()
    notify = Notify()
    assistant.add(notify)
    _notify_attention(assistant, "ada", "home", Reply("reply", "hello", Route.LOCAL))
    assert notify.pending("ada") == []
