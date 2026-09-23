import json

from robin.enroll import Enrollment, boot, enroll_on_boot
from robin.http import Service, dispatch
from robin.provision import EXE_EXEC, create_private_instance
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

API_TOKEN = "exe1.SUPERSECRETTOKEN"
MAILBOX = "mailbox-password-xyz"
PRIVATE_SECRET = "private-vault-secret-qq"
TOKEN = "abc123token"
JOINT = "https://house.example"
ADDRESS = "https://robin-ada.exe.xyz"


class Captured:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response if response is not None else {"https_url": ADDRESS}
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, url: str, command: str, api_token: str) -> dict:
        self.calls.append((url, command, api_token))
        if self.error is not None:
            raise self.error
        return self.response


def _service(post: Captured, store: HouseholdStore | None = None) -> tuple[Service, Enrollment]:
    assistant = Assistant(store=store)
    assistant.broker.put("household", "exe", API_TOKEN)
    assistant.broker.put("ada", "mailbox", MAILBOX)
    enrollment = Enrollment(store, token_factory=lambda: TOKEN)
    service = Service(assistant, object(), enrollment=enrollment, joint_url=JOINT, exe_post=post)
    return service, enrollment


def test_create_waits_for_confirm_and_enroll_burns_the_token(tmp_path) -> None:
    post = Captured()
    service, _enrollment = _service(post)
    held, held_body = dispatch(service, "POST", "/v1/private", body={"account_id": "ada"})
    assert held == 200
    assert held_body == {"status": "confirm", "https_url": None, "ready": False}
    assert post.calls == []

    status, body = dispatch(service, "POST", "/v1/private", body={"account_id": "ada", "confirm": True})
    assert status == 200
    assert body == {"status": "created", "https_url": ADDRESS, "ready": False}
    assert TOKEN not in json.dumps(body)
    assert API_TOKEN not in json.dumps(body)
    assert MAILBOX not in json.dumps(body)
    url, command, api_token = post.calls[0]
    assert url == EXE_EXEC
    assert api_token == API_TOKEN
    assert API_TOKEN not in command
    assert MAILBOX not in command
    assert PRIVATE_SECRET not in command
    assert TOKEN in command
    assert JOINT in command
    assert "\n" not in command

    waiting, waiting_body = dispatch(service, "GET", "/v1/private", query={"account_id": "ada"})
    assert waiting == 200
    assert waiting_body["ready"] is False
    missing, _missing_body = dispatch(service, "GET", "/v1/private", query={"account_id": "bea"})
    assert missing == 404

    token_path = tmp_path / "enroll.token"
    joint_path = tmp_path / "joint.url"
    token_path.write_text(TOKEN)
    joint_path.write_text(JOINT + "\n")
    enrolled: list[tuple] = []

    def enroll_post(url: str, payload: dict) -> dict:
        enrolled.append((url, payload))
        code, response = dispatch(service, "POST", "/v1/enroll", body=payload)
        assert code == 200
        return response

    served: list[str] = []
    boot(token_path=token_path, joint_path=joint_path, post=enroll_post, serve=lambda: served.append("up"))
    assert not token_path.exists()
    assert joint_path.exists()
    assert enrolled == [(JOINT + "/v1/enroll", {"token": TOKEN})]
    assert MAILBOX not in json.dumps(enrolled)
    assert served == ["up"]

    ready, ready_body = dispatch(service, "GET", "/v1/private", query={"account_id": "ada"})
    assert ready == 200
    assert ready_body == {"https_url": ADDRESS, "ready": True}
    again, again_body = dispatch(service, "POST", "/v1/enroll", body={"token": TOKEN})
    assert again == 409
    assert again_body == {"error": "token rejected"}
    assert "password" not in json.dumps(ready_body)


def test_a_failed_create_or_enroll_does_not_leave_a_live_token(tmp_path) -> None:
    post = Captured(response={"https_url": "http://insecure.example"})
    service, enrollment = _service(post)
    status, body = dispatch(service, "POST", "/v1/private", body={"account_id": "ada", "confirm": True})
    assert status == 502
    assert body == {"error": "exe.dev did not return an https url"}
    assert enrollment.get("ada") is None
    rejected, _rejected_body = dispatch(service, "POST", "/v1/enroll", body={"token": TOKEN})
    assert rejected == 409

    token_path = tmp_path / "enroll.token"
    joint_path = tmp_path / "joint.url"
    token_path.write_text(TOKEN)
    joint_path.write_text(JOINT)

    def boom(url: str, payload: dict) -> dict:
        raise RuntimeError("down")

    try:
        enroll_on_boot(token_path, joint_path, boom)
    except RuntimeError as exc:
        assert str(exc) == "down"
        assert TOKEN not in str(exc)
    else:
        raise AssertionError("enroll should fail")
    assert token_path.read_text() == TOKEN

    served: list[str] = []
    boot(token_path=tmp_path / "missing", joint_path=joint_path, post=boom, serve=lambda: served.append("up"))
    assert served == ["up"]


def test_enrollment_survives_a_restart_without_writing_the_token_in_the_clear(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    post = Captured()
    result = create_private_instance(
        enrollment=Enrollment(store, token_factory=lambda: TOKEN),
        account_id="ada",
        confirmed=True,
        joint_url=JOINT,
        post=post,
        api_token=API_TOKEN,
    )
    assert result.ready is False
    assert result.command is not None
    assert MAILBOX not in result.command
    store.close()

    raw = path.read_bytes()
    assert TOKEN.encode() not in raw
    assert API_TOKEN.encode() not in raw
    assert MAILBOX.encode() not in raw
    assert PRIVATE_SECRET.encode() not in raw

    restored = Enrollment(HouseholdStore(path, key))
    assert restored.get("ada") == {"account_id": "ada", "https_url": ADDRESS, "ready": False}
    assert restored.accept(TOKEN)["ready"] is True
    assert TOKEN.encode() not in path.read_bytes()
    again = Enrollment(HouseholdStore(path, key))
    assert again.get("ada")["ready"] is True
    try:
        again.accept(TOKEN)
    except Exception as exc:
        assert TOKEN not in str(exc)
    else:
        raise AssertionError("token should be dead")
