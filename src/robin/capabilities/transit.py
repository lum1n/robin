"""Transit trip planning via Entur. From/to leave the machine (egress)."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool

_QUERY = """
query($from: String!, $to: String!, $when: String) {
  trip(from: {name: $from}, to: {name: $to}, dateTime: $when, numTripPatterns: 3) {
    tripPatterns {
      duration
      legs { mode fromPlace { name } toPlace { name } expectedStartTime expectedEndTime }
    }
  }
}
"""


class Transit(Capability):
    id = "transit"
    tools = [
        Tool(
            name="transit_trip",
            description=(
                "Plan a public transit trip. from and to may be place names, or the aliases home and work. "
                "when is an optional ISO datetime."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "when": {"type": "string"},
                },
                "required": ["from", "to"],
            },
            effect=Effect.READ,
            egress=True,
        ),
    ]
    fields = [
        FieldSpec("summary", FieldClass.ORDINARY, free_text=True),
        FieldSpec("duration", FieldClass.ORDINARY),
    ]

    def __init__(
        self,
        *,
        home: str | None = None,
        work: str | None = None,
        post: Any = None,
    ) -> None:
        self.home = home or os.environ.get("ROBIN_HOME", "")
        self.work = work or os.environ.get("ROBIN_WORK", "")
        self._post = post or entur_post

    def status(self, account_id: str) -> str:
        return "transit: Entur"

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name != "transit_trip":
            raise NotImplementedError(tool_name)
        origin = self._alias(str(arguments.get("from", "")))
        destination = self._alias(str(arguments.get("to", "")))
        when = str(arguments.get("when", "")).strip() or None
        payload = self._post(_QUERY, {"from": origin, "to": destination, "when": when})
        patterns = (((payload.get("data") or {}).get("trip") or {}).get("tripPatterns")) or []
        rows: list[dict[str, str]] = []
        for pattern in patterns:
            legs = pattern.get("legs") or []
            parts = []
            for leg in legs:
                mode = leg.get("mode") or "leg"
                start = ((leg.get("fromPlace") or {}).get("name")) or ""
                end = ((leg.get("toPlace") or {}).get("name")) or ""
                parts.append(f"{mode} {start}->{end}")
            rows.append(
                {
                    "summary": "; ".join(parts),
                    "duration": str(pattern.get("duration") or ""),
                }
            )
        if not rows:
            return "No trips found."
        return Result(text=f"Trips {origin} to {destination}:", records=rows)

    def _alias(self, value: str) -> str:
        key = value.strip().casefold()
        if key == "home" and self.home:
            return self.home
        if key == "work" and self.work:
            return self.work
        return value.strip()


def entur_post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = Request(
        "https://api.entur.io/journey-planner/v3/graphql",
        data=body,
        headers={
            "Content-Type": "application/json",
            "ET-Client-Name": "robin-household",
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode())
