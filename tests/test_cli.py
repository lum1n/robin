from pathlib import Path

from robin.__main__ import main
from robin.model import ModelTurn
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


def test_redact_restore_and_decide(tmp_path: Path, capsys) -> None:
    vault_path = tmp_path / "vault"
    key_path = tmp_path / "key"
    assert (
        main(
            [
                "redact",
                "--text",
                f"hello {SECRET}",
                "--account",
                "ada",
                "--conversation",
                "cli",
                "--vault-out",
                str(vault_path),
                "--key-out",
                str(key_path),
            ]
        )
        == 0
    )
    redacted = capsys.readouterr().out
    assert SECRET not in redacted
    assert main(["decide", "--text", "buy milk", "--allow-cloud"]) == 0
    assert '"route": "cloud"' in capsys.readouterr().out
    assert (
        main(
            [
                "restore",
                "--text",
                "[REDACTED]",
                "--vault",
                str(vault_path),
                "--key",
                str(key_path),
                "--account",
                "ada",
                "--conversation",
                "cli",
            ]
        )
        == 0
    )


def test_exe_token_is_stored_once_and_the_file_is_removed(tmp_path: Path, capsys) -> None:
    path = tmp_path / "house.sqlite"
    key_path = tmp_path / "key"
    token_path = tmp_path / "exe.token"
    key = new_key()
    key_path.write_bytes(key)
    token = "exe1.SUPERSECRETTOKEN"
    token_path.write_text(token + "\n")
    assert main(["exe-token", "--store", str(path), "--key", str(key_path), "--token-file", str(token_path)]) == 0
    printed = capsys.readouterr().out
    assert printed.strip() == '{"stored": true}'
    assert token not in printed
    assert not token_path.exists()
    assert token.encode() not in path.read_bytes()
    revived = Assistant(store=HouseholdStore(path, key))
    assert revived.broker.reveal("household", "exe") == token

    wrong = tmp_path / "again.token"
    other = tmp_path / "other.key"
    wrong.write_text(token)
    other.write_bytes(new_key())
    try:
        main(["exe-token", "--store", str(path), "--key", str(other), "--token-file", str(wrong)])
    except Exception as exc:
        assert token not in str(exc)
    else:
        raise AssertionError("wrong key stored a token")
    assert wrong.read_text() == token
    empty = tmp_path / "empty.token"
    empty.write_text("\n")
    try:
        main(["exe-token", "--store", str(path), "--key", str(key_path), "--token-file", str(empty)])
    except SystemExit as exc:
        assert token not in str(exc)
    assert empty.exists()


def test_tick_checks_enabled_accounts_and_prints_no_secret(tmp_path: Path, capsys, monkeypatch) -> None:
    path = tmp_path / "house.sqlite"
    key_path = tmp_path / "key"
    key = new_key()
    key_path.write_bytes(key)
    store = HouseholdStore(path, key)
    assistant = Assistant(store=store)
    assistant.set_schedule("ada", True)
    assistant.broker.put("ada", "mailbox", "mailbox-password-ada")
    store.close()

    class Quiet:
        prompts: list[str] = []

        def __init__(self, base_url: str = "http://127.0.0.1:8080", transport=None, model: str = "local") -> None:
            return None

        def complete(self, *, system: str, user: str, tools: list[dict]) -> ModelTurn:
            Quiet.prompts.append(user)
            return ModelTurn("mailbox-password-ada should stay out of the journal")

    monkeypatch.setattr("robin.model.ChatModel", Quiet)
    assert main(["tick", "--store", str(path), "--key", str(key_path)]) == 0
    printed = capsys.readouterr().out
    assert '"checked": 1' in printed
    assert "mailbox-password-ada" not in printed
    assert Quiet.prompts
    assert "mailbox-password-ada" not in Quiet.prompts[0]

    def boom(*_args, **_kwargs):
        raise AssertionError("model")

    monkeypatch.setattr("robin.model.ChatModel", boom)
    assert main(["tick", "--store", str(tmp_path / "missing.sqlite"), "--key", str(key_path)]) == 0
    assert '"checked": 0' in capsys.readouterr().out
