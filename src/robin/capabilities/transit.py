"""Transit trip planning: Entur for Norway, Transitous elsewhere. From/to leave the machine (egress)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool

_CLIENT_NAME = "robin-household"
_MODES = ("rail", "bus", "tram", "metro", "water", "coach", "air")
_WORLD_MODES = {
    "rail": "RAIL",
    "bus": "BUS",
    "tram": "TRAM",
    "metro": "SUBWAY",
    "water": "FERRY",
    "coach": "COACH",
    "air": "AIRPLANE",
}
_TRANSITOUS = "https://api.transitous.org/api"

_QUERY = """
query($from: Location!, $to: Location!, $when: DateTime, $modes: Modes) {
  trip(from: $from, to: $to, dateTime: $when, modes: $modes, numTripPatterns: 3) {
    tripPatterns {
      duration
      expectedStartTime
      expectedEndTime
      legs {
        mode
        expectedStartTime
        expectedEndTime
        fromPlace { name }
        toPlace { name }
        line { publicCode }
      }
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
                "Plan a public transit trip and get the next departures, in Norway or abroad. from and to may be "
                "stop names, addresses, or the aliases home and work. when is an optional ISO datetime "
                "(defaults to now). mode limits the trip to one kind of transport, e.g. rail for trains."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "when": {"type": "string"},
                    "mode": {"type": "string", "enum": list(_MODES)},
                },
                "required": ["from", "to"],
            },
            effect=Effect.READ,
            egress=True,
        ),
    ]
    fields = [
        FieldSpec("summary", FieldClass.ORDINARY, free_text=True),
        FieldSpec("depart", FieldClass.ORDINARY),
        FieldSpec("arrive", FieldClass.ORDINARY),
        FieldSpec("duration", FieldClass.ORDINARY),
    ]

    def __init__(
        self,
        *,
        home: str | None = None,
        work: str | None = None,
        post: Any = None,
        geocode: Any = None,
        world_geocode: Any = None,
        world_plan: Any = None,
    ) -> None:
        self.home = home or os.environ.get("ROBIN_HOME", "")
        self.work = work or os.environ.get("ROBIN_WORK", "")
        self._post = post or entur_post
        self._geocode = geocode or entur_geocode
        self._world_geocode = world_geocode or transitous_geocode
        self._world_plan = world_plan or transitous_plan

    def status(self, account_id: str) -> str:
        return "transit: Entur (Norway), Transitous (elsewhere)"

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name != "transit_trip":
            raise NotImplementedError(tool_name)
        origin = self._alias(str(arguments.get("from", "")))
        destination = self._alias(str(arguments.get("to", "")))
        if not origin or not destination:
            return "Both from and to are required."
        when = str(arguments.get("when", "")).strip() or None
        mode = str(arguments.get("mode", "")).strip().casefold()
        if mode not in _MODES:
            mode = ""
        source = self._geocode(origin)
        target = self._geocode(destination)
        if source and target and "NOR" in (source.get("country", "NOR"), target.get("country", "NOR")):
            answer = self._entur_trip(source, target, when, mode)
            if answer is not None:
                return answer
        return self._world_trip(origin, destination, when, mode)

    def _entur_trip(
        self, source: dict[str, Any], target: dict[str, Any], when: str | None, mode: str
    ) -> str | Result | None:
        modes = {"transportModes": [{"transportMode": mode}]} if mode else None
        payload = self._post(
            _QUERY, {"from": _entur_location(source), "to": _entur_location(target), "when": when, "modes": modes}
        )
        errors = payload.get("errors") or []
        if errors:
            message = "; ".join(str(e.get("message", e)) for e in errors)
            return f"Entur returned an error: {message}"
        patterns = (((payload.get("data") or {}).get("trip") or {}).get("tripPatterns")) or []
        rows: list[dict[str, str]] = []
        for pattern in patterns:
            parts = []
            for leg in pattern.get("legs") or []:
                leg_mode = leg.get("mode") or "leg"
                if leg_mode == "foot":
                    continue
                code = ((leg.get("line") or {}).get("publicCode")) or ""
                start = ((leg.get("fromPlace") or {}).get("name")) or ""
                end = ((leg.get("toPlace") or {}).get("name")) or ""
                label = f"{leg_mode} {code}".strip()
                parts.append(
                    f"{_clock(leg.get('expectedStartTime'))} {label} {start}->{end} "
                    f"(arr {_clock(leg.get('expectedEndTime'))})"
                )
            rows.append(
                {
                    "summary": "; ".join(parts) or "walk",
                    "depart": _stamp(pattern.get("expectedStartTime")),
                    "arrive": _stamp(pattern.get("expectedEndTime")),
                    "duration": _minutes(pattern.get("duration")),
                }
            )
        if not rows:
            return None
        return Result(text=f"Trips {source['name']} to {target['name']}:", records=rows)

    def _world_trip(self, origin: str, destination: str, when: str | None, mode: str) -> str | Result:
        try:
            source = self._world_geocode(origin)
            if source is None:
                return f"Could not find a place called {origin}."
            target = self._world_geocode(destination)
            if target is None:
                return f"Could not find a place called {destination}."
            payload = self._world_plan(source, target, when, _WORLD_MODES.get(mode))
        except OSError as error:
            return f"Transit lookup failed: {error}"
        rows: list[dict[str, str]] = []
        for itinerary in (payload.get("itineraries") or [])[:3]:
            parts = []
            for leg in itinerary.get("legs") or []:
                leg_mode = str(leg.get("mode") or "leg")
                if leg_mode in ("WALK", "BIKE", "CAR"):
                    continue
                start = leg.get("from") or {}
                end = leg.get("to") or {}
                label = f"{leg_mode.casefold().replace('_', ' ')} {leg.get('routeShortName') or ''}".strip()
                parts.append(
                    f"{_clock(leg.get('startTime'), start.get('tz'))} {label} "
                    f"{start.get('name', '')}->{end.get('name', '')} "
                    f"(arr {_clock(leg.get('endTime'), end.get('tz'))})"
                )
            rows.append(
                {
                    "summary": "; ".join(parts) or "walk",
                    "depart": _stamp(itinerary.get("startTime"), source.get("tz")),
                    "arrive": _stamp(itinerary.get("endTime"), target.get("tz")),
                    "duration": _minutes(itinerary.get("duration")),
                }
            )
        if not rows:
            return f"No trips found from {source['name']} to {target['name']}."
        return Result(text=f"Trips {source['name']} to {target['name']}:", records=rows)

    def _alias(self, value: str) -> str:
        key = value.strip().casefold()
        if key == "home" and self.home:
            return self.home
        if key == "work" and self.work:
            return self.work
        return value.strip()


def _parse(value: Any, tz: str | None = None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if tz and moment.tzinfo is not None:
        try:
            moment = moment.astimezone(ZoneInfo(tz))
        except (KeyError, ValueError):
            pass
    return moment


def _clock(value: Any, tz: str | None = None) -> str:
    moment = _parse(value, tz)
    return moment.strftime("%H:%M") if moment else ""


def _stamp(value: Any, tz: str | None = None) -> str:
    moment = _parse(value, tz)
    return moment.strftime("%Y-%m-%d %H:%M") if moment else ""


def _entur_location(location: dict[str, Any]) -> dict[str, Any]:
    return {key: location[key] for key in ("place", "coordinates", "name") if key in location}


def _minutes(value: Any) -> str:
    try:
        return f"{round(int(value) / 60)} min"
    except (TypeError, ValueError):
        return ""


def pick_location(features: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer a stop place (what the journey planner routes best), else the top hit's coordinates."""
    if not features:
        return None
    for feature in features:
        props = feature.get("properties") or {}
        place_id = str(props.get("id", ""))
        if place_id.startswith("NSR:StopPlace:"):
            return {
                "place": place_id,
                "name": props.get("label") or props.get("name") or place_id,
                "country": props.get("country_a") or "NOR",
            }
    top = features[0]
    props = top.get("properties") or {}
    coords = (top.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2:
        return None
    return {
        "coordinates": {"latitude": coords[1], "longitude": coords[0]},
        "name": props.get("label") or props.get("name") or "",
        "country": props.get("country_a") or "NOR",
    }


def entur_geocode(text: str) -> dict[str, Any] | None:
    query = urlencode({"text": text, "size": 5, "lang": "no"})
    request = Request(
        f"https://api.entur.io/geocoder/v1/autocomplete?{query}",
        headers={"ET-Client-Name": _CLIENT_NAME},
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310
        payload = json.loads(response.read().decode())
    return pick_location(payload.get("features") or [])


def entur_post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = Request(
        "https://api.entur.io/journey-planner/v3/graphql",
        data=body,
        headers={
            "Content-Type": "application/json",
            "ET-Client-Name": _CLIENT_NAME,
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode())


def pick_world_location(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Trust the Transitous geocoder's ranking; same-named stops abroad make stop-first picks unsafe."""
    if not results:
        return None
    chosen = results[0]
    if chosen.get("lat") is None or chosen.get("lon") is None:
        return None
    return {
        "lat": chosen["lat"],
        "lon": chosen["lon"],
        "name": chosen.get("name") or "",
        "tz": chosen.get("tz"),
    }


def _transitous_get(path: str, params: dict[str, Any]) -> Any:
    request = Request(
        f"{_TRANSITOUS}{path}?{urlencode(params)}",
        headers={"User-Agent": _CLIENT_NAME},
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode())


def transitous_geocode(text: str) -> dict[str, Any] | None:
    return pick_world_location(_transitous_get("/v1/geocode", {"text": text}) or [])


def transitous_plan(
    source: dict[str, Any], target: dict[str, Any], when: str | None, mode: str | None
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "fromPlace": f"{source['lat']},{source['lon']}",
        "toPlace": f"{target['lat']},{target['lon']}",
    }
    if when:
        params["time"] = when
    if mode:
        params["transitModes"] = mode
    return _transitous_get("/v5/plan", params)
