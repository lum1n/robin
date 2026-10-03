import json
import re

from robin.loop import converse
from robin.model import ModelTurn
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.profile import (
    dump_profile,
    filled_keys,
    merge_profile,
    normalize_field,
    parse_profile,
    profile_line,
    profile_terms,
    profile_value,
    secret_values,
)
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import Vault, new_key


def test_profile_round_trips_through_the_broker(tmp_path) -> None:
    key = new_key()
    path = tmp_path / "house.sqlite"
    assistant = Assistant(store=HouseholdStore(path, key))
    saved = assistant.set_profile(
        "ada",
        {
            "given_name": "Ada",
            "family_name": "Lovelace",
            "email": "ada@example.com",
            "phone": "+47 900 00 000",
        },
    )
    assert saved["full_name"] == "Ada Lovelace"
    assert assistant.profile_presence("ada") == [
        "given_name",
        "family_name",
        "full_name",
        "email",
        "phone",
    ]
    again = Assistant(store=HouseholdStore(path, key))
    assert again.get_profile("ada")["email"] == "ada@example.com"


def test_profile_helpers_normalize_and_hide_empty() -> None:
    assert normalize_field("E-post") == "email"
    assert normalize_field("fornavn") == "given_name"
    fields = merge_profile({}, {"given_name": "Ada", "family_name": "Lovelace", "bogus": "x"})
    assert fields["full_name"] == "Ada Lovelace"
    assert "bogus" not in dump_profile(fields)
    assert profile_value(fields, "name") == "Ada Lovelace"
    assert filled_keys(parse_profile("{}")) == []
    assert secret_values(fields)[0] in {"Ada Lovelace", "Ada", "Lovelace"}


def test_http_profile_stays_on_the_account(tmp_path) -> None:
    from robin.http import Service, dispatch
    from robin.model import ModelTurn

    class Quiet:
        def complete(self, *, messages, tools):
            return ModelTurn("ok")

    assistant = Assistant(store=HouseholdStore(tmp_path / "house.sqlite", new_key()))
    service = Service(assistant, Quiet())
    service.auth.register("ada", "ada-session-password")
    service.auth.register("bea", "bea-session-password")
    ada = {"authorization": f"Bearer {service.auth.login('ada', 'ada-session-password')}"}
    bea = {"authorization": f"Bearer {service.auth.login('bea', 'bea-session-password')}"}
    stolen = dispatch(
        service,
        "POST",
        "/v1/profile",
        body={"account_id": "ada", "fields": {"email": "ada@example.com"}},
        headers=bea,
    )
    assert stolen[0] == 401
    saved = dispatch(
        service,
        "POST",
        "/v1/profile",
        body={
            "account_id": "ada",
            "fields": {
                "given_name": "Ada",
                "family_name": "Lovelace",
                "email": "ada@example.com",
                "phone": "+47 900 00 000",
            },
        },
        headers=ada,
    )
    assert saved[0] == 200
    assert saved[1]["fields"]["email"] == "ada@example.com"
    assert "email" in saved[1]["present"]
    loaded = dispatch(service, "GET", "/v1/profile", query={"account_id": "ada"}, headers=ada)
    assert loaded[1]["fields"]["given_name"] == "Ada"
    bea_view = dispatch(service, "GET", "/v1/profile", query={"account_id": "ada"}, headers=bea)
    assert bea_view[0] == 401


def test_stored_address_is_answered_to_the_person() -> None:
    class AvailableNer(UnavailableNer):
        def available(self) -> bool:
            return True

    class Scripted:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        def complete(self, *, messages, tools):
            self.seen.append([dict(message) for message in messages])
            prompt = messages[0]["content"]
            match = re.search(r"address (\[ADDRESS_[0-9a-f]{32}_\d+\])", prompt)
            assert match, prompt
            return ModelTurn(f"Your address is {match.group(1)}.")

    address = "Kirkegata 4, 7013 Trondheim"
    assistant = Assistant(ner=AvailableNer())
    assistant.set_profile("ada", {"address": address, "city": "Trondheim"})
    model = Scripted()
    reply = converse(assistant, Task("ada", "home", "what is my address?", allow_cloud=True), model)
    assert address in reply.text
    assert "that detail" not in reply.text.lower()
    seen = json.dumps(model.seen)
    assert address not in seen
    assert "Kirkegata" not in seen
    assert "[ADDRESS_" in seen


def test_profile_line_tokenizes_and_terms_share_one_address() -> None:
    fields = {"address": "Kirkegata 4, 7013 Trondheim", "city": "Trondheim"}
    vault = Vault("ada", "home")
    line = profile_line(fields, vault)
    assert "Kirkegata" not in line
    address = re.search(r"address (\[ADDRESS_[0-9a-f]{32}_\d+\])", line).group(1)
    assert vault.restore(address, strict=True) == "Kirkegata 4, 7013 Trondheim"
    terms = profile_terms(fields)
    assert [term.label for term in terms] == ["ADDRESS", "ADDRESS"]
    assert terms[0].text == "Kirkegata 4, 7013 Trondheim"
