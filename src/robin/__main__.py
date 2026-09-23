"""Inspect redaction from the command line. Nothing here opens a network connection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from robin.airlock import redact
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.session import Assistant
from robin.vault import Vault, new_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="robin")
    sub = parser.add_subparsers(dest="command", required=True)

    redact_cmd = sub.add_parser("redact")
    redact_cmd.add_argument("--text", required=True)
    redact_cmd.add_argument("--account", default="local")
    redact_cmd.add_argument("--conversation", default="default")
    redact_cmd.add_argument("--vault-out")
    redact_cmd.add_argument("--key-out")

    restore_cmd = sub.add_parser("restore")
    restore_cmd.add_argument("--text", required=True)
    restore_cmd.add_argument("--vault", required=True)
    restore_cmd.add_argument("--key", required=True)
    restore_cmd.add_argument("--account", required=True)
    restore_cmd.add_argument("--conversation", required=True)

    decide_cmd = sub.add_parser("decide")
    decide_cmd.add_argument("--text", required=True)
    decide_cmd.add_argument("--account", default="local")
    decide_cmd.add_argument("--conversation", default="default")
    decide_cmd.add_argument("--allow-cloud", action="store_true")
    decide_cmd.add_argument("--free-text", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "redact":
        vault = Vault(args.account, args.conversation)
        redacted, _report = redact(args.text, vault)
        if args.vault_out:
            key = new_key()
            Path(args.vault_out).write_bytes(vault.encrypt(key))
            if not args.key_out:
                raise SystemExit("redact --vault-out requires --key-out")
            Path(args.key_out).write_bytes(key)
        print(redacted)
        return 0
    if args.command == "restore":
        vault = Vault.decrypt(
            Path(args.vault).read_bytes(),
            Path(args.key).read_bytes().strip(),
            account_id=args.account,
            conversation_id=args.conversation,
        )
        print(vault.restore(args.text))
        return 0
    assistant = Assistant(ner=UnavailableNer())
    decision = assistant.decide(
        Task(
            account_id=args.account,
            conversation_id=args.conversation,
            text=args.text,
            allow_cloud=args.allow_cloud,
            free_text=args.free_text,
        )
    )
    print(json.dumps({"route": decision.route.value, "reason": decision.reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
