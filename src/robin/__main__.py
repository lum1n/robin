"""Command line. redact, restore, and decide do not open a network connection. boot enrolls, then serves."""

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

    exe_cmd = sub.add_parser("exe-token")
    exe_cmd.add_argument("--store", required=True)
    exe_cmd.add_argument("--key", required=True)
    exe_cmd.add_argument("--token-file", required=True)

    tick_cmd = sub.add_parser("tick")
    tick_cmd.add_argument("--store", required=True)
    tick_cmd.add_argument("--key", required=True)
    tick_cmd.add_argument("--model", default="http://127.0.0.1:8080")

    boot_cmd = sub.add_parser("boot")
    boot_cmd.add_argument("--token", default="/etc/robin/enroll.token")
    boot_cmd.add_argument("--joint", default="/etc/robin/joint.url")
    boot_cmd.add_argument("--store")
    boot_cmd.add_argument("--key")
    boot_cmd.add_argument("--advertise", default="")
    boot_cmd.add_argument("--advertise-file", default="")
    boot_cmd.add_argument("--host", default="127.0.0.1")
    boot_cmd.add_argument("--port", type=int, default=8787)

    args = parser.parse_args(argv)
    if args.command == "boot":
        return _boot(args)
    if args.command == "tick":
        return _tick(args)
    if args.command == "exe-token":
        return _exe_token(args)
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


def _exe_token(args: argparse.Namespace) -> int:
    token_path = Path(args.token_file)
    if not token_path.is_file():
        raise SystemExit("exe token file is missing")
    token = token_path.read_text().strip()
    if not token:
        raise SystemExit("exe token file is empty")
    from robin.store import HouseholdStore

    try:
        store = HouseholdStore(args.store, Path(args.key).read_bytes().strip())
        assistant = Assistant(store=store)
        assistant.broker.put("household", "exe", token)
        store.close()
    except Exception as exc:
        if token in str(exc):
            raise SystemExit("exe token was rejected") from None
        raise
    token_path.unlink()
    print(json.dumps({"stored": True}))
    return 0


def _tick(args: argparse.Namespace) -> int:
    from robin.capabilities.install import install
    from robin.model import ChatModel
    from robin.schedule import tick
    from robin.store import HouseholdStore

    store_path = Path(args.store)
    key_path = Path(args.key)
    if not store_path.is_file() or not key_path.is_file():
        print(json.dumps({"checked": 0, "confirm": 0}))
        return 0
    store = HouseholdStore(store_path, key_path.read_bytes().strip())
    assistant = Assistant(store=store)
    install(assistant)
    replies = tick(assistant, ChatModel(base_url=args.model))
    print(json.dumps({"checked": len(replies), "confirm": sum(reply.status == "confirm" for reply in replies)}))
    return 0


def joint_url(advertise: str, advertise_file: str) -> str:
    url = advertise
    if not url and advertise_file:
        path = Path(advertise_file)
        if path.is_file():
            url = path.read_text().strip()
    if not url:
        return ""
    from robin.provision import _safe_url

    if not _safe_url(url):
        raise SystemExit("advertise url must be http or https")
    return url


def _boot(args: argparse.Namespace) -> int:
    from robin.capabilities.install import install
    from robin.enroll import Enrollment, enroll_on_boot, urllib_enroll_post
    from robin.http import Service, serve
    from robin.model import ChatModel
    from robin.provision import urllib_exe_post
    from robin.store import HouseholdStore

    advertised = joint_url(args.advertise, args.advertise_file)
    enroll_on_boot(args.token, args.joint, urllib_enroll_post)
    store = None
    if args.store:
        if not args.key:
            raise SystemExit("boot --store requires --key")
        store = HouseholdStore(args.store, Path(args.key).read_bytes().strip())
    assistant = Assistant(store=store)
    install(assistant)
    serve(
        Service(
            assistant,
            ChatModel(),
            enrollment=Enrollment(store),
            joint_url=advertised,
            exe_post=urllib_exe_post if advertised else None,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
