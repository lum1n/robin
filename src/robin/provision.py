"""Plan an exe.dev VM. The create call is recorded without credentials."""

from __future__ import annotations

from dataclasses import dataclass

PINNED_IMAGE = "robin/exeuntu:pinned"
_SCRIPT_LIMIT = 10 * 1024


@dataclass(frozen=True)
class ProvisionPlan:
    status: str
    recorded: str | None


def setup_script(enroll_token: str) -> str:
    if not enroll_token.isalnum():
        raise ValueError("enroll token must be alphanumeric")
    script = (
        "#!/bin/sh\n"
        "umask 077\n"
        "mkdir -p /etc/robin\n"
        f"printf '%s' '{enroll_token}' > /etc/robin/enroll.token\n"
        "systemctl enable --now robin\n"
    )
    if len(script.encode()) > _SCRIPT_LIMIT:
        raise ValueError("setup script exceeds 10KiB")
    return script


def plan_private_instance(*, account_id: str, confirmed: bool, enroll_token: str) -> ProvisionPlan:
    if not confirmed:
        return ProvisionPlan(status="confirm", recorded=None)
    name = _safe_name(account_id)
    script = setup_script(enroll_token)
    recorded = (
        "new --json --no-email "
        f"--name=robin-{name} "
        f"--image={PINNED_IMAGE} "
        f"--setup-script={script}"
    )
    return ProvisionPlan(status="planned", recorded=recorded)


def _safe_name(account_id: str) -> str:
    if not account_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in account_id):
        raise ValueError("account id must be a lowercase slug")
    return account_id
