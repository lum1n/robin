from pathlib import Path

from robin.__main__ import main

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
