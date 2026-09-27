"""Route a task. The model is remote. The airlock decides what it may see. The gate is code, not a second model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from robin.airlock import Report


class Route(Enum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class Task:
    account_id: str
    conversation_id: str
    text: str
    allow_cloud: bool = False
    free_text: bool = False


@dataclass(frozen=True)
class Decision:
    route: Route
    reason: str
    redacted: str
    local_text: str
    cloud_payload: str | None


def route_for(report: Report, *, allow_cloud: bool) -> tuple[Route, str]:
    if report.has_critical:
        return Route.LOCAL, "critical value present"
    if report.unresolved:
        return Route.LOCAL, "unresolved sensitive text"
    if not allow_cloud:
        return Route.LOCAL, "cloud not requested"
    return Route.CLOUD, "opt-in and clean"


def decide(task: Task, report: Report, *, redacted: str, local_text: str) -> Decision:
    route, reason = route_for(report, allow_cloud=task.allow_cloud)
    return Decision(
        route=route,
        reason=reason,
        redacted=redacted,
        local_text=local_text,
        cloud_payload=redacted if route is Route.CLOUD else None,
    )
