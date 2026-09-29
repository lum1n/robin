"""Egress canary: seeded PII must never appear in outbound model request bodies."""

from __future__ import annotations

from robin.airlock import VocabularyTerm
from robin.capabilities.groceries import Lists
from robin.capabilities.mail import ImapMailbox, Mail, mailbox_secret
from robin.capabilities.screen import Screen
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.session import Assistant

SEED = {
    "name": "Kari Nordmann",
    "email": "kari.nordmann@example.com",
    "fnr": "01010000110",
    "card": "4111111111111111",
    "password": "hunter2-secret-password",
    "phone": "+4712345678",
}


class ReadyNer(UnavailableNer):
    def available(self) -> bool:
        return True


class Recording:
    def __init__(self) -> None:
        self.bodies: list[str] = []
        self.turns = [
            ModelTurn("", (ToolCall("lists_show", {"list": "groceries"}),)),
            ModelTurn("", (ToolCall("screen_read", {}),)),
            ModelTurn("All clear."),
        ]

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.bodies.append(str(messages))
        return self.turns.pop(0)


class FakeBox:
    def connected(self, account_id: str) -> bool:
        return True

    def messages(self, account_id: str) -> list[dict[str, str]]:
        return [
            {
                "id": "1",
                "sender": SEED["name"],
                "subject": "hello",
                "body": f"call me at {SEED['phone']} card {SEED['card']}",
            }
        ]


def test_seeded_pii_never_reaches_the_model() -> None:
    assistant = Assistant(ner=ReadyNer())
    assistant.set_vocabulary("ada", (VocabularyTerm(SEED["name"]), VocabularyTerm(SEED["email"], "EMAIL")))
    assistant.add(
        Lists(
            members={"ada"},
            shared=[{"item": "milk", "list": "groceries", "loyalty": SEED["password"]}],
            private={},
        )
    )
    assistant.add(Screen(owner="ada", text=f"id {SEED['fnr']} mail {SEED['email']}", password=SEED["password"]))
    model = Recording()
    converse(
        assistant,
        Task("ada", "t", f"help {SEED['name']} with the list and screen", allow_cloud=True),
        model,
    )
    blob = "\n".join(model.bodies)
    for value in SEED.values():
        assert value not in blob, value
