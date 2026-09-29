from robin.profile import (
    dump_profile,
    filled_keys,
    merge_profile,
    normalize_field,
    parse_profile,
    profile_value,
    secret_values,
)
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


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
