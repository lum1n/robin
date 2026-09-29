import threading

from robin.airlock import VocabularyTerm
from robin.capabilities.groceries import Groceries
from robin.model import ModelTurn
from robin.policy import Task
from robin.loop import converse
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import VaultAccessError, new_key

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


class Scripted:
    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        return ModelTurn("Noted, Jane Doe.")


def test_a_request_thread_can_store_an_account(tmp_path) -> None:
    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    errors: list[BaseException] = []

    def work() -> None:
        try:
            store.ensure_account("ada")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert errors == []
    assert store.accounts() == ["ada"]


def test_a_restart_keeps_one_accounts_thread_and_hides_it_from_the_other(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    assistant = Assistant(store=store)
    assistant.set_vocabulary("ada", (VocabularyTerm("Jane Doe"),))
    assistant.add(Groceries(members={"ada"}, shared=[{"item": "milk", "loyalty": ""}], private={}))
    converse(assistant, Task("ada", "kitchen", "hello Jane Doe"), Scripted())
    assistant.invoke("ada", "kitchen", "lists_show", {"note": SECRET})
    store.close()

    assert b"Jane Doe" not in path.read_bytes()
    assert SECRET.encode() not in path.read_bytes()

    revived = Assistant(store=HouseholdStore(path, key))
    assert revived.vaults.get("ada", "kitchen").restore("[PERSON_1]") == "Jane Doe"
    assert revived.threads("ada") == ["kitchen"]
    assert revived.threads("bea") == []
    texts = [turn["text"] for turn in revived.turns("ada", "kitchen")]
    assert "hello Jane Doe" in texts
    assert "Noted, Jane Doe." in texts
    assert revived.turns("bea", "kitchen") == []
    activity = revived.activity.read("ada")
    assert activity[-1]["tool"] == "lists_show"
    assert SECRET not in activity[-1]["arguments"]
    assert revived.activity.read("bea") == []
    assert revived.vocabulary["ada"][0].text == "Jane Doe"


def test_the_wrong_key_cannot_open_the_store(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    store = HouseholdStore(path, new_key())
    Assistant(store=store).decide(Task("ada", "kitchen", "hello"))
    store.close()
    try:
        Assistant(store=HouseholdStore(path, new_key()))
    except VaultAccessError:
        return
    raise AssertionError("wrong key opened the store")
