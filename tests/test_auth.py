from robin.auth import Auth, AuthError
from robin.http import Service, dispatch
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

PASSWORD = "correct-horse-battery"
TOKEN = "session-token-ada"


def test_a_session_is_required_and_belongs_to_one_account(tmp_path) -> None:
    key = new_key()
    store = HouseholdStore(tmp_path / "house.sqlite", key)
    auth = Auth(store, token_factory=lambda: TOKEN)
    service = Service(Assistant(store=store), object(), auth=auth)
    created, created_body = dispatch(
        service,
        "POST",
        "/v1/accounts",
        body={"account_id": "ada", "password": PASSWORD},
    )
    assert created == 201
    assert created_body == {"account_id": "ada"}
    assert PASSWORD not in str(created_body)
    again, again_body = dispatch(
        service,
        "POST",
        "/v1/accounts",
        body={"account_id": "ada", "password": "other-password"},
    )
    assert again == 409
    assert "other-password" not in str(again_body)

    failed, failed_body = dispatch(
        service,
        "POST",
        "/v1/sessions",
        body={"account_id": "ada", "password": "nope"},
    )
    assert failed == 401
    assert failed_body == {"error": "login failed"}
    assert PASSWORD not in str(failed_body)

    logged, logged_body = dispatch(
        service,
        "POST",
        "/v1/sessions",
        body={"account_id": "ada", "password": PASSWORD},
    )
    assert logged == 200
    assert logged_body == {"token": TOKEN}
    headers = {"authorization": f"Bearer {TOKEN}"}
    missing, _missing_body = dispatch(
        service,
        "POST",
        "/v1/messages",
        body={"account_id": "ada", "conversation_id": "kitchen", "text": "hello"},
    )
    assert missing == 401
    opened, _opened_body = dispatch(
        service,
        "GET",
        "/v1/threads",
        query={"account_id": "ada"},
        headers=headers,
    )
    assert opened == 200
    foreign, _foreign_body = dispatch(
        service,
        "GET",
        "/v1/threads",
        query={"account_id": "bea"},
        headers=headers,
    )
    assert foreign == 401

    store.close()
    raw = (tmp_path / "house.sqlite").read_bytes()
    assert PASSWORD.encode() not in raw
    assert TOKEN.encode() not in raw
    restored = Auth(HouseholdStore(tmp_path / "house.sqlite", key))
    assert restored.account(TOKEN) == "ada"
    assert restored.login("ada", PASSWORD) != ""
    try:
        restored.register("ada", PASSWORD)
    except AuthError as exc:
        assert PASSWORD not in str(exc)
    else:
        raise AssertionError("password should already be set")
