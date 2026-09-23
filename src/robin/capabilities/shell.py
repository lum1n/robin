"""A shell on this instance. The working directory is that account's files."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from robin.capability import Capability, Effect, FieldSpec, Tool

_LIMIT = 8000


class ShellBox:
    def __init__(self, root: Path, runner: Any = None) -> None:
        self.root = root
        self.runner = runner or run_command

    def run(self, account_id: str, command: str) -> str:
        if not command.strip() or "\x00" in command:
            raise ValueError("command is required")
        if not account_id or "/" in account_id or account_id in {".", ".."}:
            raise ValueError("account is required")
        work = (self.root / account_id).resolve()
        work.mkdir(parents=True, exist_ok=True)
        return self.runner(command, work)[:_LIMIT]


class Terminal(Capability):
    id = "shell"
    tools = [
        Tool(
            name="run_command",
            description="Run a command in this account's directory. Waits for confirmation.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            effect=Effect.EXTERNAL,
        )
    ]
    fields: list[FieldSpec] = []

    def __init__(self, box: ShellBox) -> None:
        self.box = box

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "run_command":
            return self.box.run(account_id, str(arguments.get("command", "")))
        raise NotImplementedError(tool_name)


def run_command(command: str, work: Path) -> str:
    result = subprocess.run(
        command,
        shell=True,
        cwd=work,
        env={"PATH": "/usr/bin:/bin", "HOME": str(work), "TMPDIR": str(work)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result.stdout + result.stderr
