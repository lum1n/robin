from robin.capabilities.groceries import Groceries
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

LOYALTY = "LOYALTY998877"
PRIVATE_ITEM = "a-private-tonic"


def test_a_shared_add_waits_and_a_private_item_stays_with_its_owner() -> None:
    groceries = Groceries(members={"ada"})
    assistant = Assistant()
    assistant.add(groceries)
    held = assistant.invoke("ada", "pantry", "add_shared", {"item": "milk"})
    assert held["status"] == "confirm"
    assert groceries.records("ada") == []
    done = assistant.invoke("ada", "pantry", "add_shared", {"item": "milk"}, confirmed=True)
    assert done["status"] == "done"
    private = assistant.invoke("ada", "pantry", "add_private", {"item": PRIVATE_ITEM})
    assert private["status"] == "done"
    assert groceries.records("ada") == [
        {"item": "milk", "loyalty": ""},
        {"item": PRIVATE_ITEM, "loyalty": ""},
    ]
    assert groceries.records("bea") == []
    invite = assistant.invoke("ada", "pantry", "add_member", {"account_id": "bea"})
    assert invite["status"] == "confirm"
    assert "bea" not in groceries.members
    assistant.invoke("ada", "pantry", "add_member", {"account_id": "bea"}, confirmed=True)
    assert groceries.records("bea") == [{"item": "milk", "loyalty": ""}]
    assert PRIVATE_ITEM not in str(groceries.records("bea"))
    try:
        groceries.invoke("cara", "add_shared", {"item": "eggs"})
    except PermissionError:
        return
    raise AssertionError("a non-member added to the household list")


def test_the_list_survives_a_restart_without_writing_items_in_the_clear(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    Groceries(
        members={"ada", "bea"},
        shared=[{"item": "milk", "loyalty": LOYALTY}],
        private={"ada": [{"item": PRIVATE_ITEM, "loyalty": ""}]},
        store=store,
    )
    store.close()
    raw = path.read_bytes()
    assert b"milk" not in raw
    assert LOYALTY.encode() not in raw
    assert PRIVATE_ITEM.encode() not in raw
    revived = Groceries(store=HouseholdStore(path, key))
    assert revived.records("ada")[0]["item"] == "milk"
    assert revived.records("bea") == [{"item": "milk", "loyalty": LOYALTY}]
    assert PRIVATE_ITEM not in str(revived.records("bea"))
    assert revived.records("ada")[1]["item"] == PRIVATE_ITEM
