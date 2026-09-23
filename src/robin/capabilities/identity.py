"""An account login on this machine. A confirmed command runs as that login."""

from __future__ import annotations

import hashlib
import os
import pwd
import subprocess
from pathlib import Path
from typing import Any

from robin.enroll import account_slug

_LOGIN_LIMIT = 32


def spawn(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, **kwargs)


def login_name(account_id: str) -> str:
    slug = account_slug(account_id)
    readable = f"robin-{slug}"
    if len(readable) <= _LOGIN_LIMIT:
        return readable
    return "r" + hashlib.sha256(slug.encode()).hexdigest()[: _LOGIN_LIMIT - 1]


def claim(account_id: str, root: Path, work: Path) -> str:
    """Create the account directory. As root, give it to that account's login."""
    login = login_name(account_id)
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    if len(root_resolved.parts) < 4:
        raise ValueError("files directory is required")
    candidate = root_resolved / account_id
    if candidate.is_symlink():
        raise ValueError("account is required")
    candidate.mkdir(exist_ok=True)
    work_resolved = candidate.resolve()
    if work_resolved != work.resolve() or work_resolved.parent != root_resolved:
        raise ValueError("account is required")
    if os.geteuid() == 0:
        os.chmod(root_resolved, 0o711)
        _ensure_user(login, work_resolved)
        owner = pwd.getpwnam(login)
        os.chown(work_resolved, owner.pw_uid, owner.pw_gid)
    os.chmod(work_resolved, 0o700)
    return login


def give(path: Path, login: str) -> None:
    if not path.exists():
        return
    if os.geteuid() == 0:
        owner = pwd.getpwnam(login)
        os.chown(path, owner.pw_uid, owner.pw_gid)
    path.chmod(0o600 if path.is_file() else 0o700)


def run_as(command: str, work: Path, login: str) -> str:
    result = spawn(
        ["runuser", "--preserve-environment", "-u", login, "--", "sh", "-c", command],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(work),
            "TMPDIR": str(work),
            "USER": login,
            "LOGNAME": login,
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result.stdout + result.stderr


def _ensure_user(login: str, home: Path) -> None:
    try:
        pwd.getpwnam(login)
        return
    except KeyError:
        pass
    result = spawn(
        [
            "useradd",
            "--system",
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            "--home-dir",
            str(home),
            login,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("account login was not created")
