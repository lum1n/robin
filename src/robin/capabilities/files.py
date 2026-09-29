"""Files on this instance. This user can read what they own. Another user's files stay closed."""

from __future__ import annotations

import os
import pwd
from collections.abc import Callable
from pathlib import Path
from typing import Any

from robin.capabilities.identity import claim, give
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool

_LIMIT = 1_000_000
_VISIT_CAP = 30_000
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
_BLOCKED_NAMES = {"robin.key", "store.key", "enroll.token"}
_SKIP_NAMES = {".git", "node_modules", ".venv", "__pycache__", ".ms-playwright"}
_SYSTEM_ROOTS = {
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/boot"),
    Path("/etc"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
    Path("/var/log"),
    Path("/var/cache"),
    Path("/var/lib/docker"),
    Path("/var/lib/containerd"),
}


class Workspace:
    def __init__(
        self,
        root: Path,
        *,
        volume: Path | None = None,
        owner: Callable[[Path], int] | None = None,
        user: int | None = None,
    ) -> None:
        self.root = root
        self.volume = volume if volume is not None else root
        self.owner = owner or _owner_uid
        self.user = user

    def names(self, account_id: str) -> list[str]:
        base = self._base(account_id)
        if not base.is_dir():
            return []
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            # The Playwright profile sits at files/<account>/browser. Walking it
            # on every decide() turns "Hei" into a multi-second cache scan.
            dirnames[:] = [
                name
                for name in dirnames
                if name not in _SKIP_NAMES and not (current == base and name == "browser")
            ]
            for filename in filenames:
                if filename in _BLOCKED_NAMES:
                    continue
                path = current / filename
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved != base and base not in resolved.parents:
                    continue
                if not resolved.is_file():
                    continue
                found.append(resolved.relative_to(base).as_posix())
        return sorted(found)

    def owned_files(self, account_id: str, *, suffixes: set[str] | None = None) -> list[Path]:
        found: list[Path] = []
        seen: set[Path] = set()
        visits = 0
        starts = [self._base(account_id)]
        home = Path.home()
        if _within(home, self.volume.resolve()):
            starts.insert(0, home)
        starts.append(self.volume)
        for start in starts:
            visits = self._walk(start, account_id, found, seen, visits, suffixes)
        return sorted(found)

    def read(self, account_id: str, relative: str) -> str:
        try:
            path = self.readable(account_id, relative)
        except ValueError as exc:
            return str(exc)
        if not path.is_file():
            return "file is missing"
        if path.suffix.lower() in IMAGE_SUFFIXES:
            return f"photo {path}"
        return path.read_text(errors="replace")[:_LIMIT]

    def readable(self, account_id: str, text: str) -> Path:
        if text.startswith("/"):
            path = Path(text).resolve()
            if not self.allowed_file(account_id, path):
                raise ValueError("path belongs to another user")
            return path
        return self.locate(account_id, text)

    def allowed_file(self, account_id: str, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        if resolved.name in _BLOCKED_NAMES or _skipped(resolved):
            return False
        if self._in_other_account(account_id, resolved):
            return False
        if self._in_account(account_id, resolved) and resolved.is_file():
            return True
        if self._behind_foreign_user(account_id, resolved):
            return False
        try:
            uid = self.owner(resolved)
        except OSError:
            return False
        return resolved.is_file() and uid in self._allowed()

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

    def _walk(
        self,
        directory: Path,
        account_id: str,
        found: list[Path],
        seen: set[Path],
        visits: int,
        suffixes: set[str] | None,
    ) -> int:
        if visits >= _VISIT_CAP:
            return visits
        try:
            resolved = directory.resolve()
        except OSError:
            return visits
        if resolved in seen or _skipped(resolved) or self._in_other_account(account_id, resolved):
            return visits
        seen.add(resolved)
        try:
            uid = self.owner(resolved)
            children = list(resolved.iterdir()) if resolved.is_dir() else []
        except OSError:
            return visits
        visits += 1
        if self._foreign(uid) and not self._in_account(account_id, resolved):
            return visits
        for child in children:
            if visits >= _VISIT_CAP:
                return visits
            if child.is_symlink() or child.name in _BLOCKED_NAMES:
                continue
            try:
                child_uid = self.owner(child)
            except OSError:
                continue
            if child.is_dir():
                visits = self._walk(child, account_id, found, seen, visits, suffixes)
                continue
            if not child.is_file():
                continue
            if suffixes is not None and child.suffix.lower() not in suffixes:
                continue
            if child_uid in self._allowed() or self._in_account(account_id, child):
                if not self._in_other_account(account_id, child):
                    found.append(child.resolve())
        return visits

    def _allowed(self) -> set[int]:
        if self.user is not None:
            return {self.user}
        uid = os.geteuid()
        if uid != 0:
            return {uid}
        name = os.environ.get("ROBIN_USER", "").strip()
        if not name:
            return set()
        try:
            return {pwd.getpwnam(name).pw_uid}
        except KeyError:
            return set()

    def _foreign(self, uid: int) -> bool:
        return uid >= 1000 and uid not in self._allowed()

    def _behind_foreign_user(self, account_id: str, path: Path) -> bool:
        for ancestor in [path, *path.parents]:
            if self._in_account(account_id, ancestor):
                return False
            try:
                uid = self.owner(ancestor)
            except OSError:
                return True
            if self._foreign(uid):
                return True
        return False

    def _in_account(self, account_id: str, path: Path) -> bool:
        base = self._base(account_id)
        resolved = path if path.is_absolute() else path.resolve()
        return resolved == base or base in resolved.parents

    def _in_other_account(self, account_id: str, path: Path) -> bool:
        root = self.root.resolve()
        resolved = path.resolve()
        if resolved == root or root not in resolved.parents:
            return False
        top = root / resolved.relative_to(root).parts[0]
        return top != self._base(account_id) and top.is_dir()


class Files(Capability):
    id = "files"
    tools = [
        Tool(
            name="files_list",
            description="List files owned by this user on this machine. Another user's files are left out.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="files_read",
            description="Read a file this user owns. Another user's path is refused.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="files_search",
            description="Search file names and text contents owned by this user for a query.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="files_write",
            description="Write a file in this account's directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                "required": ["path", "text"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="files_delete",
            description="Delete a file in this account's directory. Waits for confirmation.",
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

    def status(self, account_id: str) -> str:
        return "files: available"

    def records(self, account_id: str) -> list[dict[str, str]]:
        return [{"name": name} for name in self.workspace.names(account_id)]

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "files_list":
            paths = self.workspace.owned_files(account_id)
            if not paths:
                return "no files"
            shown = [str(path) for path in paths[:100]]
            body = "\n".join(shown)
            if len(paths) > len(shown):
                body += f"\n{len(shown)} of {len(paths)}"
            return body
        if tool_name == "files_read":
            return self.workspace.read(account_id, str(arguments.get("path", "")))
        if tool_name == "files_search":
            return self._search(account_id, str(arguments.get("query", "")))
        if tool_name == "files_write":
            self.workspace.write(account_id, str(arguments.get("path", "")), str(arguments.get("text", "")))
            return "wrote"
        if tool_name == "files_delete":
            return self.workspace.delete(account_id, str(arguments.get("path", "")))
        raise NotImplementedError(tool_name)

    def _search(self, account_id: str, query: str) -> str:
        needle = query.strip().casefold()
        if not needle:
            return "query is required"
        hits: list[str] = []
        for path in self.workspace.owned_files(account_id):
            name = str(path)
            if needle in name.casefold():
                hits.append(name)
                continue
            if path.suffix.lower() in IMAGE_SUFFIXES:
                continue
            try:
                text = path.read_text(errors="replace")[:_LIMIT]
            except OSError:
                continue
            if needle in text.casefold():
                hits.append(name)
            if len(hits) >= 50:
                break
        if not hits:
            return "no files matched"
        return "\n".join(hits)


def _owner_uid(path: Path) -> int:
    return path.stat().st_uid


def _within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _skipped(path: Path) -> bool:
    if path in _SYSTEM_ROOTS or any(root in path.parents for root in _SYSTEM_ROOTS):
        return True
    return path.name in _SKIP_NAMES
