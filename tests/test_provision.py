from robin.provision import PINNED_IMAGE, plan_private_instance, setup_script
from robin.session import Assistant

API_TOKEN = "exe1.SUPERSECRETTOKEN"
MAILBOX = "mailbox-password-xyz"
PRIVATE_SECRET = "private-vault-secret-qq"
ENROLL = "abc123token"
JOINT = "https://house.example"


def test_create_is_held_until_confirm_and_the_call_carries_no_secrets() -> None:
    held = plan_private_instance(account_id="ada", confirmed=False, enroll_token=ENROLL)
    assert held.status == "confirm"
    assert held.recorded is None

    planned = plan_private_instance(account_id="ada", confirmed=True, enroll_token=ENROLL, joint_url=JOINT)
    assert planned.recorded is not None
    assert PINNED_IMAGE in planned.recorded
    assert "--setup-script" in planned.recorded
    assert "--no-email" in planned.recorded
    assert "--json" in planned.recorded
    assert "share set-public robin-ada" in planned.recorded
    assert ENROLL in planned.recorded
    assert JOINT in planned.recorded
    assert "\\n" in planned.recorded
    assert "\n" not in planned.recorded
    assert API_TOKEN not in planned.recorded
    assert MAILBOX not in planned.recorded
    assert PRIVATE_SECRET not in planned.recorded
    assert len(setup_script(ENROLL, JOINT).encode()) < 10 * 1024

    assistant = Assistant()
    assistant.broker.put("household", "exe", API_TOKEN)
    assistant.broker.put("ada", "mailbox", MAILBOX)
    assert not assistant.vaults.get("ada", "joint").contains_value(PRIVATE_SECRET)
    assert not assistant.vaults.get("ada", "joint").contains_value(MAILBOX)
    assert API_TOKEN not in planned.recorded
