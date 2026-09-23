"""Files on this instance. Each account has its own directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from robin.capabilities.identity import claim, give
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool

_LIMIT = 1_000_000


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    def names(self, account_id: str) -> list[str]:
        base = self._base(account_id)
        if not base.is_dir():
            return []
        found: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved != base and base not in resolved.parents:
                continue
            found.append(resolved.relative_to(base).as_posix())
        return sorted(found)

    def read(self, account_id: str, relative: str) -> str:
        path = self.locate(account_id, relative)
        if not path.is_file():
            return "file is missing"
        return path.read_text(errors="replace")[:_LIMIT]

    def write(self, account_id: str, relative: str, text: str) -> None:
        if len(text.encode()) > _LIMIT:
            raise ValueError("file is too large")
        path = self.locate(account_id, relative)
        base = self._base(account_id)
        login = claim(account_id, self.root, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        current = base
        give(current, login)
        for part in path.relative_to(base).parts:
            current = current / part
            give(current, login)

    def delete(self, account_id: str, relative: str) -> str:
        path = self.locate(account_id, relative)
        if not path.is_file():
            return "file is missing"
        path.unlink()
        return "deleted"

    def locate(self, account_id: str, relative: str) -> Path:
        base = self._base(account_id)
        if not relative or relative.startswith("/") or "\x00" in relative or "\\" in relative:
            raise ValueError("path must stay in this account")
        if any(part in {"", ".", ".."} for part in Path(relative).parts):
            raise ValueError("path must stay in this account")
        path = (base / relative).resolve()
        if path != base and base not in path.parents:
            raise ValueError("path must stay in this account")
        return path

    def _base(self, account_id: str) -> Path:
        if not account_id or account_id in {".", ".."} or "/" in account_id or "\\" in account_id:
            raise ValueError("account is required")
        return (self.root / account_id).resolve()


class Files(Capability):
    id = "files"
    tools = [
        Tool(
            name="list_files",
            description="List files in this account's directory.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="read_file",
            description="Read a file in this account's directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="write_file",
            description="Write a file in this account's directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                "required": ["path", "text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="delete_file",
            description="Delete a file in this account's directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [FieldSpec("name", FieldClass.ORDINARY)]

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def records(self, account_id: str) -> list[dict[str, str]]:
        return [{"name": name} for name in self.workspace.names(account_id)]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_files":
            return f"{len(self.records(account_id))} files"
        if tool_name == "read_file":
            return self.workspace.read(account_id, str(arguments.get("path", "")))
        if tool_name == "write_file":
            self.workspace.write(account_id, str(arguments.get("path", "")), str(arguments.get("text", "")))
            return "wrote"
        if tool_name == "delete_file":
            return self.workspace.delete(account_id, str(arguments.get("path", "")))
        raise NotImplementedError(tool_name)
