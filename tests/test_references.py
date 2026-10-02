import json

import pytest

from robin.airlock import Entity, VocabularyTerm, redact
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool, render_records
from robin.loop import converse, resume
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import REFERENCE, ReferenceError, Vault, new_key


class ReadyNer(UnavailableNer):
    def available(self) -> bool:
        return True


class Echo(Capability):
    id = "echo"
    tools = [
        Tool("echo", "", {"type": "object"}, Effect.READ),
        Tool("send", "", {"type": "object"}, Effect.READ, egress=True),
        Tool("fail", "", {"type": "object"}, Effect.READ),
    ]
    fields = []

    def __init__(self) -> None:
        self.calls = []

    def invoke(self, account_id, tool_name, arguments):
        self.calls.append((account_id, tool_name, arguments))
        if tool_name == "fail":
            raise ValueError(f"Failed for {arguments['name']}; jane@example.com")
        return json.dumps(arguments)


def test_scoped_references_are_stable_typed_and_isolated():
    one = Vault("ada", "one")
    two = Vault("ada", "two")
    other = Vault("bea", "one")
    reference = one.token("PERSON", "Jane Doe")
    assert reference == one.token("PERSON", "Jane Doe")
    assert reference != one.token("PERSON", "Bob Berg")
    assert reference != one.token("ORG", "Jane Doe")
    assert reference != two.token("PERSON", "Jane Doe")
    assert reference != other.token("PERSON", "Jane Doe")
    assert one.restore(reference, strict=True) == "Jane Doe"
    for vault in (two, other):
        with pytest.raises(ReferenceError, match="foreign"):
            vault.restore(reference, strict=True)


def test_restore_never_recursively_expands_values():
    vault = Vault("ada", "one")
    email = vault.token("EMAIL", "jane@example.com")
    text = vault.token("TEXT", f"literal {email}")
    assert vault.restore(f"{text} / {email}", strict=True) == f"literal {email} / jane@example.com"
    assert redact(f"literal {email}", vault)[0] == text


@pytest.mark.parametrize("value", [
    "[PERSON_1]", "[EMAIL_999]", "[PERSON_0]", "[PERSON_bad_1]",
    "[EMAIL_1234_1]", "[PERSON_", "[PERSON_1",
    "[person_1]", "[EMAIL bad 1]", "[PERSON]", "[EMAIL:123]",
    "[REDACTED]", "[UNRESOLVED]", "[redacted]", "[UNRESOLVED",
])
def test_invalid_references_block_nested_arguments_before_execution(value):
    assistant = Assistant(ner=ReadyNer())
    echo = Echo()
    assistant.add(echo)
    for confirmed in (False, True):
        outcome = assistant.invoke(
            "ada", "one", "send",
            {"payload": [{"value": value}]}, confirmed=confirmed, for_model=True,
        )
        assert outcome["status"] == "error"
        assert "Action blocked" in outcome["result"]
    assert echo.calls == []
    assert assistant.activity.read("ada")[-1]["arguments"] == "blocked: invalid reference"


def test_changed_type_and_foreign_references_are_not_resolved():
    assistant = Assistant()
    echo = Echo()
    assistant.add(echo)
    reference = assistant.vaults.get("ada", "one").token("EMAIL", "jane@example.com")
    for account, thread, value in (
        ("ada", "two", reference),
        ("bea", "one", reference),
        ("ada", "one", reference.replace("EMAIL", "PERSON")),
    ):
        assert assistant.invoke(account, thread, "echo", {"value": value})["status"] == "error"
    assert echo.calls == []


def test_argument_keys_resolve_and_conflicting_keys_block_execution():
    assistant = Assistant()
    echo = Echo()
    assistant.add(echo)
    reference = assistant.vaults.get("ada", "one").token("TEXT", "recipient")
    held = assistant.invoke("ada", "one", "send", {reference: "value"})
    assert held["status"] == "confirm"
    assert echo.calls == []
    done = assistant.invoke("ada", "one", "send", {reference: "value"}, confirmed=True)
    assert done["status"] == "done"
    assert echo.calls[-1][2] == {"recipient": "value"}
    for arguments in ({reference: "one", "recipient": "two"}, {"nested": {"[TEXT_999]": "value"}}):
        assert assistant.invoke("ada", "one", "echo", arguments)["status"] == "error"
    assert len(echo.calls) == 1


def test_redaction_is_idempotent_and_known_values_survive_detector_misses():
    vault = Vault("ada", "one")
    original = "Jane Doe <jane@example.com>"
    first, _ = redact(original, vault, vocabulary=(VocabularyTerm("Jane Doe"),))
    assert "Jane Doe" not in first
    again, _ = redact(first, vault, extra=(Entity(0, len(first), "PERSON"),))
    assert again == first
    missed, _ = redact(original, vault)
    assert missed == first
    assert vault.restore(first) == original
    rows, _ = render_records(
        [{"name": vault.token("PERSON", "Jane Doe")}],
        [FieldSpec("name", FieldClass.TOKENIZE, "PERSON")], vault,
    )
    assert rows[0]["name"] == vault.token("PERSON", "Jane Doe")


def test_detector_type_drift_cannot_change_a_known_reference():
    vault = Vault("ada", "one")
    reference = vault.token("ORG", "Some Shop")
    shown, _ = redact("Some Shop", vault, extra=(Entity(0, 9, "PERSON"),))
    assert shown == reference
    assert vault.restore(shown) == "Some Shop"


def test_malformed_reference_cannot_hide_a_secret_from_redaction():
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    shown, report = redact(f"[TEXT_{secret}]", Vault("ada", "one"))
    assert secret not in shown
    assert report.has_critical


def test_results_and_errors_reuse_the_same_mapping():
    assistant = Assistant(ner=ReadyNer())
    echo = Echo()
    assistant.add(echo)
    vault = assistant.vaults.get("ada", "one")
    name = vault.token("PERSON", "Jane Doe")
    email = vault.token("EMAIL", "jane@example.com")
    for tool in ("echo", "fail"):
        outcome = assistant.invoke("ada", "one", tool, {"name": name}, for_model=True)
        assert echo.calls[-1][2] == {"name": "Jane Doe"}
        assert name in outcome["result"]
        assert "Jane Doe" not in outcome["result"]
        assert "jane@example.com" not in outcome["result"]
        if tool == "fail":
            assert email in outcome["result"]
    assert "Jane Doe" not in json.dumps(assistant.activity.read("ada"))


def test_results_and_activity_redact_mapped_values_before_json_escaping():
    assistant = Assistant()
    echo = Echo()
    assistant.add(echo)
    original = 'private "quoted"\nvalue'
    reference = assistant.vaults.get("ada", "one").token("TEXT", original)
    outcome = assistant.invoke("ada", "one", "echo", {reference: [reference]}, for_model=True)
    assert json.loads(outcome["result"]) == {reference: [reference]}
    assert "private" not in outcome["result"]
    logged = assistant.activity.read("ada")[-1]["arguments"]
    assert "private" not in logged
    assert json.loads(logged) == {reference: [reference]}


def test_legacy_vault_migrates_without_accepting_model_aliases():
    payload = {
        "account_id": "ada", "conversation_id": "one",
        "values": {"[PERSON_1]": "Jane Doe", "[EMAIL_1]": "jane@example.com"},
        "counts": {"PERSON": 1, "EMAIL": 1},
    }
    vault = Vault.from_dump(payload)
    reference = vault.token("PERSON", "Jane Doe")
    assert reference != "[PERSON_1]"
    assert vault.restore("[PERSON_1]") == "Jane Doe"
    assert vault.canonicalize("[PERSON_1]") == reference
    with pytest.raises(ReferenceError):
        vault.restore("[PERSON_1]", strict=True)
    shown, _ = redact("[PERSON_1]", vault)
    assert shown == reference
    revived = Vault.from_dump(vault.dump())
    assert revived.token("PERSON", "Jane Doe") == reference
    assert revived.restore("[PERSON_1]") == "Jane Doe"
    key = new_key()
    opened = Vault.decrypt(vault.encrypt(key), key, account_id="ada", conversation_id="one")
    assert opened.restore(reference, strict=True) == "Jane Doe"
    assert opened.token("PERSON", "Another Person") != reference


def test_pending_legacy_confirmation_survives_a_restart(tmp_path):
    key = new_key()
    path = tmp_path / "house.sqlite"
    store = HouseholdStore(path, key)
    assistant = Assistant(store=store)
    assistant.vaults.put(Vault.from_dump({
        "account_id": "ada", "conversation_id": "one",
        "values": {"[PERSON_1]": "Jane Doe"}, "counts": {"PERSON": 1},
    }))
    assistant.persist_vault("ada", "one")
    assistant.set_pending("ada", "one", "send", {"name": "[PERSON_1]"}, "local")
    store.close()
    revived = Assistant(store=HouseholdStore(path, key))
    echo = Echo()
    revived.add(echo)
    reply = resume(revived, "ada", "one")
    assert reply.status == "reply"
    assert echo.calls == [("ada", "send", {"name": "Jane Doe"})]
    revived.store.close()


def test_agent_round_trip_preserves_two_distinct_values_across_turns():
    assistant = Assistant(ner=ReadyNer())
    echo = Echo()
    assistant.add(echo)
    first = "jane@example.com"
    second = "bob@example.com"

    class Model:
        def __init__(self):
            self.step = 0
            self.references = None

        def complete(self, *, messages, tools):
            serialized = json.dumps(messages)
            assert first not in serialized and second not in serialized
            references = [match.group() for match in REFERENCE.finditer(messages[1]["content"])]
            if self.references is None:
                self.references = references
            assert references == self.references
            self.step += 1
            if self.step % 2:
                return ModelTurn("", (ToolCall("echo", {"to": references[1], "cc": [references[0]]}),))
            result = json.loads(messages[-1]["content"])
            assert result == {"to": references[1], "cc": [references[0]]}
            return ModelTurn(f"To {references[1]}, cc {references[0]}.")

    model = Model()
    for _ in range(2):
        reply = converse(assistant, Task("ada", "one", f"Use {first} and {second}"), model)
        assert reply.text == f"To {second}, cc {first}."
        assert echo.calls[-1][2] == {"to": second, "cc": [first]}


def test_agent_receives_explicit_reference_error_without_running_the_tool():
    assistant = Assistant(ner=ReadyNer())
    echo = Echo()
    assistant.add(echo)

    class Model:
        def complete(self, *, messages, tools):
            if messages[-1]["role"] != "tool":
                return ModelTurn("", (ToolCall("echo", {"value": "[EMAIL_999]"}),))
            assert "Action blocked" in messages[-1]["content"]
            return ModelTurn("I need a valid recipient before I can do that.")

    reply = converse(assistant, Task("ada", "one", "Send that"), Model())
    assert echo.calls == []
    assert "valid recipient" in reply.text
