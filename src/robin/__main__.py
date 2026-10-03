"""Command line. redact, restore, and decide do not open a network connection. boot enrolls, then serves."""

from __future__ import annotations

import argparse
import json
import os
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
    tick_cmd.add_argument(
        "--model-provider",
        choices=("openai", "nearai"),
        default="openai",
        help="openai: OpenAI-compatible URL. nearai: NEAR AI Cloud TEE models + privacy filter.",
    )
    tick_cmd.add_argument("--model-name", default="local")
    tick_cmd.add_argument("--model-url", default="")

    boot_cmd = sub.add_parser("boot")
    boot_cmd.add_argument("--token", default="/etc/robin/enroll.token")
    boot_cmd.add_argument("--joint", default="/etc/robin/joint.url")
    boot_cmd.add_argument("--store")
    boot_cmd.add_argument("--key")
    boot_cmd.add_argument("--advertise", default="")
    boot_cmd.add_argument("--advertise-file", default="")
    boot_cmd.add_argument("--host", default="127.0.0.1")
    boot_cmd.add_argument("--port", type=int, default=8787)
    boot_cmd.add_argument("--model-url", default="http://127.0.0.1:8080")
    boot_cmd.add_argument("--model-name", default="local")
    boot_cmd.add_argument(
        "--model-provider",
        choices=("openai", "nearai"),
        default="openai",
        help="openai: OpenAI-compatible URL. nearai: NEAR AI Cloud TEE models + privacy filter.",
    )
    boot_cmd.add_argument("--mcp-config", default="/etc/robin/mcp.json")
    boot_cmd.add_argument(
        "--ner-model",
        default="",
        help="GLiNER checkpoint. Empty uses the default; 'no' selects the Norwegian model.",
    )

    probe_cmd = sub.add_parser("browser-probe", help="Open known sites and report bot walls vs ok.")
    probe_cmd.add_argument("--headless", action="store_true", help="Force headless Chromium.")
    probe_cmd.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="Extra URL to probe (repeatable). Defaults to a fixed list when omitted.",
    )

    args = parser.parse_args(argv)
    if args.command == "boot":
        return _boot(args)
    if args.command == "browser-probe":
        return _browser_probe(args)
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
    model_url = args.model_url or args.model
    replies = tick(
        assistant,
        _build_model(
            provider=args.model_provider,
            model_url=model_url,
            model_name=args.model_name,
            api_key=os.environ.get("ROBIN_MODEL_KEY", ""),
        ),
    )
    print(json.dumps({"checked": len(replies), "confirm": sum(reply.status == "confirm" for reply in replies)}))
    return 0


def _build_model(*, provider: str, model_url: str, model_name: str, api_key: str):
    """Build the configured Model. nearai requires a key and only accepts TEE chat models."""
    from robin.model import (
        NEARAI_BASE,
        NEARAI_DEFAULT_MODEL,
        ChatModel,
        NearAiModel,
        TeeModelError,
    )

    if provider == "nearai":
        if not api_key.strip():
            raise SystemExit("nearai model provider requires ROBIN_MODEL_KEY")
        url = model_url if model_url and model_url != "http://127.0.0.1:8080" else NEARAI_BASE
        name = model_name if model_name and model_name != "local" else NEARAI_DEFAULT_MODEL
        model = NearAiModel(url, model=name, api_key=api_key)
        try:
            model.ensure_tee()
        except TeeModelError as exc:
            raise SystemExit(str(exc)) from None
        except Exception as exc:
            raise SystemExit(f"Robin could not verify that the model runs in a TEE: {exc}") from None
        return model
    return ChatModel(model_url, model=model_name, api_key=api_key)


def _start_clock(assistant: Assistant, model) -> None:
    import threading
    import time

    from robin.schedule import run_due

    def loop() -> None:
        while True:
            try:
                run_due(assistant, model)
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=loop, name="robin-clock", daemon=True).start()


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
    from robin.ner import DEFAULT_MODEL, GlinerNer, NORWEGIAN_MODEL
    from robin.provision import urllib_exe_post
    from robin.store import HouseholdStore

    advertised = joint_url(args.advertise, args.advertise_file)
    enroll_on_boot(args.token, args.joint, urllib_enroll_post)
    store = None
    if args.store:
        if not args.key:
            raise SystemExit("boot --store requires --key")
        store = HouseholdStore(args.store, Path(args.key).read_bytes().strip())
    model_name = DEFAULT_MODEL
    if args.ner_model == "no":
        model_name = NORWEGIAN_MODEL
    elif args.ner_model:
        model_name = args.ner_model
    ner = GlinerNer(model_name)
    ner.warm()
    assistant = Assistant(store=store, ner=ner)
    install(assistant, mcp_config=getattr(args, "mcp_config", "") or "")
    if not ner._installed:
        print(
            "robin: WARNING local NER packages are missing — reinstall robin "
            "(gliner is a core dependency). Without NER, conversation history and "
            "free-text tool results stay [UNRESOLVED] and browser controls become "
            "unusable labels.",
            flush=True,
        )
    elif ner._failed:
        print("robin: WARNING local NER failed to load — free-text cloud egress stays blocked", flush=True)
    else:
        print("robin: loading local NER in the background", flush=True)
    model = _build_model(
        provider=args.model_provider,
        model_url=args.model_url,
        model_name=args.model_name,
        api_key=os.environ.get("ROBIN_MODEL_KEY", ""),
    )
    if args.model_provider == "nearai":
        print(f"robin: near.ai TEE model {getattr(model, 'model', args.model_name)}", flush=True)
    _start_clock(assistant, model)
    serve(
        Service(
            assistant,
            model,
            enrollment=Enrollment(store),
            joint_url=advertised,
            exe_post=urllib_exe_post if advertised else None,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


_PROBE_URLS = (
    "https://www.skyscanner.com/",
    "https://www.lot.com/",
    "https://www.kiwi.com/",
    "https://flybillet.no/",
    "https://www.google.com/travel/flights",
    "https://www.finn.no/",
    "https://www.vg.no/",
    "https://bot.sannysoft.com/",
)


def _browser_probe(args: argparse.Namespace) -> int:
    import os

    from robin.capabilities.browser import _bot_wall_note, launch_options, open_chromium
    from robin.capabilities.vdisplay import browser_engine

    if args.headless:
        os.environ["ROBIN_BROWSER_HEADLESS"] = "1"
    urls = tuple(args.urls) if args.urls else _PROBE_URLS
    options = launch_options()
    print(
        json.dumps(
            {
                "engine": browser_engine(),
                "headless": options["headless"],
                "channel": options.get("channel"),
                "ignore_default_args": options["ignore_default_args"],
            }
        ),
        flush=True,
    )
    rows: list[dict[str, str]] = []
    for url in urls:
        row: dict[str, str] = {"url": url, "status": "error", "detail": ""}
        page = None
        try:
            page = open_chromium(url, account_id="probe")
            text, _ = page.read()
            landed = ""
            try:
                landed = page.location()
            except Exception:
                landed = ""
            wall = _bot_wall_note(text)
            empty = "content:\n(empty)" in text.lower()
            if wall:
                row["status"] = "walled"
                row["detail"] = wall.split("\n", 1)[0][:200]
            elif empty:
                row["status"] = "empty"
            else:
                row["status"] = "ok"
            row["landed"] = landed
        except Exception as exc:
            row["detail"] = str(exc)[:300]
        finally:
            if page is not None:
                for closer in ("_context", "_browser"):
                    obj = getattr(page, closer, None)
                    if obj is None:
                        continue
                    try:
                        obj.close()
                    except Exception:
                        pass
                pw = getattr(page, "_playwright", None)
                if pw is not None:
                    try:
                        pw.stop()
                    except Exception:
                        pass
        rows.append(row)
        print(json.dumps(row), flush=True)
    summary = {
        "ok": sum(1 for row in rows if row["status"] == "ok"),
        "walled": sum(1 for row in rows if row["status"] == "walled"),
        "empty": sum(1 for row in rows if row["status"] == "empty"),
        "error": sum(1 for row in rows if row["status"] == "error"),
    }
    print(json.dumps({"summary": summary}), flush=True)
    return 0 if summary["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
