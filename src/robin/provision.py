"""Plan an exe.dev VM. The create call is recorded without credentials."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from robin.enroll import Enrollment, account_slug

PINNED_IMAGE = "nimul/robin:pinned"
EXE_EXEC = "https://exe.dev/exec"
_SCRIPT_LIMIT = 10 * 1024


@dataclass(frozen=True)
class ProvisionPlan:
    status: str
    recorded: str | None


@dataclass(frozen=True)
class CreateResult:
    status: str
    https_url: str | None
    ready: bool
    command: str | None


def setup_script(enroll_token: str, joint_url: str) -> str:
    if not enroll_token.isalnum():
        raise ValueError("enroll token must be alphanumeric")
    if not _safe_url(joint_url):
        raise ValueError("joint url must be http or https")
    script = (
        "#!/bin/sh\n"
        "umask 077\n"
        "sudo mkdir -p /etc/robin\n"
        f"sudo sh -c \"printf '%s' '{enroll_token}' > /etc/robin/enroll.token\"\n"
        f"sudo sh -c \"printf '%s' '{joint_url}' > /etc/robin/joint.url\"\n"
        "sudo chmod 600 /etc/robin/enroll.token /etc/robin/joint.url\n"
        "sudo systemctl enable --now robin robin-tick.timer\n"
    )
    if len(script.encode()) > _SCRIPT_LIMIT:
        raise ValueError("setup script exceeds 10KiB")
    return script


def exe_command(account_id: str, enroll_token: str, joint_url: str) -> str:
    name = account_slug(account_id)
    script = setup_script(enroll_token, joint_url).replace("\\", "\\\\").replace("'", "'\\''").replace("\n", "\\n")
    created = (
        "new --json --no-email "
        f"--name=robin-{name} "
        f"--image={PINNED_IMAGE} "
        f"--setup-script='{script}'"
    )
    return f"out=$({created}) && share set-public robin-{name} >/dev/null 2>&1 && printf '%s' \"$out\""


def plan_private_instance(
    *,
    account_id: str,
    confirmed: bool,
    enroll_token: str,
    joint_url: str = "",
) -> ProvisionPlan:
    if not confirmed:
        return ProvisionPlan(status="confirm", recorded=None)
    return ProvisionPlan(status="planned", recorded=exe_command(account_id, enroll_token, joint_url))


def delete_command(account_id: str) -> str:
    return f"rm robin-{account_slug(account_id)} --json"


def delete_private_instance(
    *,
    enrollment: Enrollment,
    account_id: str,
    confirmed: bool,
    post: Any,
    api_token: str,
) -> CreateResult:
    record = enrollment.get(account_id)
    if record is None:
        raise LookupError("private instance is missing")
    if not confirmed:
        return CreateResult(status="confirm", https_url=record["https_url"], ready=bool(record["ready"]), command=None)
    if not api_token:
        raise RuntimeError("exe token is missing")
    command = delete_command(account_id)
    try:
        post(EXE_EXEC, command, api_token)
    except Exception as exc:
        if api_token and api_token in str(exc):
            raise RuntimeError("exe.dev delete failed") from None
        raise
    enrollment.forget(account_id)
    return CreateResult(status="deleted", https_url=None, ready=False, command=command)


def create_private_instance(
    *,
    enrollment: Enrollment,
    account_id: str,
    confirmed: bool,
    joint_url: str,
    post: Any,
    api_token: str,
) -> CreateResult:
    if not confirmed:
        return CreateResult(status="confirm", https_url=None, ready=False, command=None)
    if not api_token:
        raise RuntimeError("exe token is missing")
    token = enrollment.issue(account_id)
    command = exe_command(account_id, token, joint_url)
    try:
        response = post(EXE_EXEC, command, api_token)
        https_url = response.get("https_url") if isinstance(response, dict) else None
        if not isinstance(https_url, str) or not https_url.startswith("https://"):
            raise RuntimeError("exe.dev did not return an https url")
        enrollment.note_address(token, https_url)
    except Exception:
        enrollment.discard(token)
        raise
    return CreateResult(status="created", https_url=https_url, ready=False, command=command)


def urllib_exe_post(url: str, command: str, api_token: str) -> dict:
    request = Request(
        url,
        data=command.encode(),
        headers={"Authorization": f"Bearer {api_token}"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read().decode())


def _safe_url(url: str) -> bool:
    if not (url.startswith("https://") or url.startswith("http://")):
        return False
    rest = url.split("://", 1)[1]
    return bool(rest) and all(char.isalnum() or char in ".-:/" for char in rest)
