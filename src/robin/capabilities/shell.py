"""A shell on this instance. The working directory is that account's files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from robin.capabilities.browser import _wants_page
from robin.capabilities.identity import claim, run_as
from robin.capability import Capability, Effect, FieldSpec, Tool

_LIMIT = 8000


class ShellBox:
    def __init__(self, root: Path, runner: Any = None) -> None:
        self.root = root
        self.runner = runner or run_as

    def run(self, account_id: str, command: str) -> str:
        if not command.strip() or "\x00" in command:
            raise ValueError("command is required")
        login = claim(account_id, self.root, self.root / account_id)
        return self.runner(command, (self.root / account_id).resolve(), login)[:_LIMIT]


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

    def offered_tools(self, account_id: str, task: str) -> list[Tool]:
        if _wants_page(task):
            return []
        return list(self.tools)

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "run_command":
            return self.box.run(account_id, str(arguments.get("command", "")))
        raise NotImplementedError(tool_name)
