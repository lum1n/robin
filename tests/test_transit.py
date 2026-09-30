from __future__ import annotations

from typing import Any

from robin.capabilities.transit import Transit, pick_location, pick_world_location
from robin.capability import Result

_PLACES = {
    "Sørumsand": {"place": "NSR:StopPlace:58864", "name": "Sørumsand stasjon", "country": "NOR"},
    "Oslo S": {"place": "NSR:StopPlace:59872", "name": "Oslo S", "country": "NOR"},
    "Stockholm C": {"place": "NSR:StopPlace:58635", "name": "Stockholm Centralstation", "country": "SWE"},
    "Uppsala C": {"place": "NSR:StopPlace:1", "name": "Uppsala Centralstation", "country": "SWE"},
}

_WORLD = {
    "Stockholm C": {"lat": 59.33, "lon": 18.06, "name": "Stockholm Central", "tz": "Europe/Stockholm"},
    "Uppsala C": {"lat": 59.86, "lon": 17.64, "name": "Uppsala Central", "tz": "Europe/Stockholm"},
    "Sørumsand": {"lat": 59.98, "lon": 11.24, "name": "Sørumsand", "tz": "Europe/Oslo"},
    "Oslo S": {"lat": 59.91, "lon": 10.75, "name": "Oslo S", "tz": "Europe/Oslo"},
}

_WORLD_PLAN = {
    "itineraries": [
        {
            "startTime": "2026-10-01T06:10:00Z",
            "endTime": "2026-10-01T06:58:00Z",
            "duration": 2880,
            "legs": [
                {"mode": "WALK", "from": {"name": "a"}, "to": {"name": "b"}},
                {
                    "mode": "LONG_DISTANCE",
                    "routeShortName": "914",
                    "startTime": "2026-10-01T06:11:00Z",
                    "endTime": "2026-10-01T06:49:00Z",
                    "from": {"name": "Stockholm Centralstation", "tz": "Europe/Stockholm"},
                    "to": {"name": "Uppsala Centralstation", "tz": "Europe/Stockholm"},
                },
            ],
        }
    ]
}

_TRIP = {
    "data": {
        "trip": {
            "tripPatterns": [
                {
                    "duration": 1560,
                    "expectedStartTime": "2026-09-30T20:30:00+02:00",
                    "expectedEndTime": "2026-09-30T20:56:00+02:00",
                    "legs": [
                        {
                            "mode": "foot",
                            "expectedStartTime": "2026-09-30T20:25:00+02:00",
                            "expectedEndTime": "2026-09-30T20:30:00+02:00",
                            "fromPlace": {"name": "Origin"},
                            "toPlace": {"name": "Sørumsand stasjon"},
                            "line": None,
                        },
                        {
                            "mode": "rail",
                            "expectedStartTime": "2026-09-30T20:30:00+02:00",
                            "expectedEndTime": "2026-09-30T20:56:00+02:00",
                            "fromPlace": {"name": "Sørumsand stasjon"},
                            "toPlace": {"name": "Oslo S"},
                            "line": {"publicCode": "R14"},
                        },
                    ],
                }
            ]
        }
    }
}


def _transit(
    payload: dict[str, Any], calls: list[dict[str, Any]], world_calls: list[tuple] | None = None
) -> Transit:
    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append(variables)
        return payload

    def plan(source: dict, target: dict, when: str | None, mode: str | None) -> dict[str, Any]:
        if world_calls is not None:
            world_calls.append((source["name"], target["name"], when, mode))
        return _WORLD_PLAN

    return Transit(
        home="Sørumsand", post=post, geocode=_PLACES.get, world_geocode=_WORLD.get, world_plan=plan
    )


def test_trip_resolves_places_and_reports_times() -> None:
    calls: list[dict[str, Any]] = []
    transit = _transit(_TRIP, calls)
    result = transit.invoke("a", "transit_trip", {"from": "home", "to": "Oslo S", "mode": "rail"})
    assert isinstance(result, Result)
    assert calls[0]["from"] == {"place": "NSR:StopPlace:58864", "name": "Sørumsand stasjon"}
    assert "country" not in calls[0]["to"]
    assert calls[0]["to"]["place"] == "NSR:StopPlace:59872"
    assert calls[0]["modes"] == {"transportModes": [{"transportMode": "rail"}]}
    row = result.records[0]
    assert row["summary"] == "20:30 rail R14 Sørumsand stasjon->Oslo S (arr 20:56)"
    assert row["depart"] == "2026-09-30 20:30"
    assert row["arrive"] == "2026-09-30 20:56"
    assert row["duration"] == "26 min"


def test_unknown_place_is_reported() -> None:
    calls: list[dict[str, Any]] = []
    transit = _transit(_TRIP, calls)
    text = transit.invoke("a", "transit_trip", {"from": "Nowhere", "to": "Oslo S"})
    assert text == "Could not find a place called Nowhere."
    assert calls == []


def test_entur_errors_are_surfaced() -> None:
    transit = _transit({"errors": [{"message": "boom"}]}, [])
    text = transit.invoke("a", "transit_trip", {"from": "Sørumsand", "to": "Oslo S"})
    assert text == "Entur returned an error: boom"


def test_unknown_mode_is_ignored() -> None:
    calls: list[dict[str, Any]] = []
    transit = _transit(_TRIP, calls)
    transit.invoke("a", "transit_trip", {"from": "Sørumsand", "to": "Oslo S", "mode": "rocket"})
    assert calls[0]["modes"] is None


def test_pick_location_prefers_stop_place() -> None:
    features = [
        {"properties": {"id": "732513", "label": "Sørumsand"}, "geometry": {"coordinates": [11.24, 59.98]}},
        {"properties": {"id": "NSR:StopPlace:58864", "label": "Sørumsand stasjon"}, "geometry": {}},
    ]
    assert pick_location(features) == {
        "place": "NSR:StopPlace:58864",
        "name": "Sørumsand stasjon",
        "country": "NOR",
    }


def test_pick_location_falls_back_to_coordinates() -> None:
    features = [{"properties": {"id": "x", "label": "Storgata 1"}, "geometry": {"coordinates": [10.75, 59.91]}}]
    assert pick_location(features) == {
        "coordinates": {"latitude": 59.91, "longitude": 10.75},
        "name": "Storgata 1",
        "country": "NOR",
    }
    assert pick_location([]) is None


def test_trip_abroad_uses_transitous_in_local_time() -> None:
    calls: list[dict[str, Any]] = []
    world: list[tuple] = []
    transit = _transit(_TRIP, calls, world)
    result = transit.invoke("a", "transit_trip", {"from": "Stockholm C", "to": "Uppsala C", "mode": "rail"})
    assert isinstance(result, Result)
    assert calls == []
    assert world == [("Stockholm Central", "Uppsala Central", None, "RAIL")]
    row = result.records[0]
    assert row["summary"] == "08:11 long distance 914 Stockholm Centralstation->Uppsala Centralstation (arr 08:49)"
    assert row["depart"] == "2026-10-01 08:10"
    assert row["duration"] == "48 min"


def test_cross_border_trip_uses_entur() -> None:
    calls: list[dict[str, Any]] = []
    world: list[tuple] = []
    transit = _transit(_TRIP, calls, world)
    transit.invoke("a", "transit_trip", {"from": "Oslo S", "to": "Stockholm C"})
    assert len(calls) == 1
    assert world == []


def test_empty_entur_result_falls_back_to_transitous() -> None:
    world: list[tuple] = []
    transit = _transit({"data": {"trip": {"tripPatterns": []}}}, [], world)
    result = transit.invoke("a", "transit_trip", {"from": "Sørumsand", "to": "Oslo S"})
    assert isinstance(result, Result)
    assert world == [("Sørumsand", "Oslo S", None, None)]


def test_transitous_network_failure_is_reported() -> None:
    def broken(text: str) -> dict[str, Any]:
        raise OSError("offline")

    transit = Transit(post=lambda q, v: {}, geocode=lambda t: None, world_geocode=broken)
    assert transit.invoke("a", "transit_trip", {"from": "Paris", "to": "Lyon"}) == "Transit lookup failed: offline"


def test_pick_world_location_uses_top_hit() -> None:
    results = [
        {"type": "PLACE", "name": "Uppsala", "lat": 59.86, "lon": 17.64, "tz": "Europe/Stockholm"},
        {"type": "STOP", "name": "Uppsala", "lat": 43.63, "lon": 3.82, "tz": "Europe/Paris"},
    ]
    assert pick_world_location(results) == {
        "lat": 59.86,
        "lon": 17.64,
        "name": "Uppsala",
        "tz": "Europe/Stockholm",
    }
    assert pick_world_location([]) is None
