import json

from robin.airlock import VocabularyTerm
from robin.http import Service, dispatch
from robin.policy import Task
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

NAME = "Jane Doe"
PASSPHRASE = "vault-passphrase-ada"


def test_a_vault_moves_to_another_instance_and_stays_off_the_other_account(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    assistant = Assistant(store=HouseholdStore(path, key))
    assistant.set_vocabulary("ada", (VocabularyTerm(NAME),))
    assistant.decide(Task("ada", "kitchen", f"hello {NAME}"))
    reference = assistant.vaults.get("ada", "kitchen").token("PERSON", NAME)
    service = Service(assistant, object())
    service.auth.register("ada", "ada-session-password")
    service.auth.register("bea", "bea-session-password")
    ada = {"authorization": f"Bearer {service.auth.login('ada', 'ada-session-password')}"}
    bea = {"authorization": f"Bearer {service.auth.login('bea', 'bea-session-password')}"}

    stolen, _stolen_body = dispatch(
        service,
        "POST",
        "/v1/export",
        body={"account_id": "ada", "passphrase": PASSPHRASE},
        headers=bea,
    )
    assert stolen == 401
    status, body = dispatch(
        service,
        "POST",
        "/v1/export",
        body={"account_id": "ada", "passphrase": PASSPHRASE},
        headers=ada,
    )
    assert status == 200
    blob = body["export"]
    rendered = json.dumps(body)
    assert NAME not in rendered
    assert PASSPHRASE not in rendered
    assert NAME.encode() not in path.read_bytes()

    other_path = tmp_path / "private.sqlite"
    other_key = new_key()
    private = Assistant(store=HouseholdStore(other_path, other_key))
    private_service = Service(private, object())
    private_service.auth.register("ada", "ada-session-password")
    private_service.auth.register("bea", "bea-session-password")
    private_ada = {"authorization": f"Bearer {private_service.auth.login('ada', 'ada-session-password')}"}
    private_bea = {"authorization": f"Bearer {private_service.auth.login('bea', 'bea-session-password')}"}
    rejected, rejected_body = dispatch(
        private_service,
        "POST",
        "/v1/import",
        body={"account_id": "bea", "passphrase": PASSPHRASE, "export": blob},
        headers=private_bea,
    )
    assert rejected == 400
    assert rejected_body["error"] == "export belongs to another account"
    assert PASSPHRASE not in json.dumps(rejected_body)
    assert private.vaults.get("bea", "kitchen").restore(reference) == reference

    wrong, wrong_body = dispatch(
        private_service,
        "POST",
        "/v1/import",
        body={"account_id": "ada", "passphrase": "nope", "export": blob},
        headers=private_ada,
    )
    assert wrong == 400
    assert wrong_body["error"] == "export rejected"
    assert "nope" not in json.dumps(wrong_body)

    imported, imported_body = dispatch(
        private_service,
        "POST",
        "/v1/import",
        body={"account_id": "ada", "passphrase": PASSPHRASE, "export": blob},
        headers=private_ada,
    )
    assert imported == 200
    assert imported_body == {"imported": 1}
    assert private.vaults.get("ada", "kitchen").restore(reference) == NAME
    assert private.vaults.get("ada", "kitchen").token("PERSON", NAME) == reference
    assert private.vocabulary["ada"][0].text == NAME
    assert NAME.encode() not in other_path.read_bytes()
    assert PASSPHRASE.encode() not in other_path.read_bytes()
