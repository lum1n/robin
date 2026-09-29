"""Weather from MET Norway Locationforecast. Place leaves the machine when set."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool


class Weather(Capability):
    id = "weather"
    tools = [
        Tool(
            name="weather_forecast",
            description=(
                "Get a weather forecast. Omit place to use the household default location. "
                "When place is set, it is sent to the weather service."
            ),
            parameters={
                "type": "object",
                "properties": {"place": {"type": "string"}},
            },
            effect=Effect.READ,
            egress=True,
        ),
    ]
    fields = [
        FieldSpec("when", FieldClass.ORDINARY),
        FieldSpec("summary", FieldClass.ORDINARY, free_text=True),
        FieldSpec("temperature", FieldClass.ORDINARY),
    ]

    def __init__(
        self,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        fetch: Any = None,
        geocode: Any = None,
    ) -> None:
        self.latitude = latitude if latitude is not None else float(os.environ.get("ROBIN_LAT", "59.91"))
        self.longitude = longitude if longitude is not None else float(os.environ.get("ROBIN_LON", "10.75"))
        self._fetch = fetch or urllib_get
        self._geocode = geocode or nominatim_geocode

    def status(self, account_id: str) -> str:
        return f"weather: default {self.latitude},{self.longitude}"

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name != "weather_forecast":
            raise NotImplementedError(tool_name)
        place = str(arguments.get("place", "")).strip()
        lat, lon = self.latitude, self.longitude
        if place:
            found = self._geocode(place)
            if found is None:
                return f"Could not locate {place}."
            lat, lon = found
        url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?{urlencode({'lat': lat, 'lon': lon})}"
        raw = self._fetch(url, headers={"User-Agent": "RobinHouseholdAssistant/1.0 github.com/nimul/robin"})
        payload = json.loads(raw)
        series = (((payload.get("properties") or {}).get("timeseries")) or [])[:8]
        rows: list[dict[str, str]] = []
        for item in series:
            details = ((item.get("data") or {}).get("instant") or {}).get("details") or {}
            summary = (((item.get("data") or {}).get("next_1_hours") or {}).get("summary") or {}).get("symbol_code") or ""
            rows.append(
                {
                    "when": str(item.get("time") or ""),
                    "summary": str(summary),
                    "temperature": str(details.get("air_temperature", "")),
                }
            )
        if not rows:
            return "No forecast available."
        label = place or f"{lat},{lon}"
        return Result(text=f"Weather for {label}:", records=rows)


def urllib_get(url: str, headers: dict[str, str] | None = None) -> str:
    request = Request(url, headers=headers or {}, method="GET")
    with urlopen(request, timeout=15) as response:  # noqa: S310
        return response.read().decode()


def nominatim_geocode(place: str) -> tuple[float, float] | None:
    url = "https://nominatim.openstreetmap.org/search?" + urlencode({"q": place, "format": "json", "limit": 1})
    request = Request(url, headers={"User-Agent": "RobinHouseholdAssistant/1.0"}, method="GET")
    with urlopen(request, timeout=15) as response:  # noqa: S310
        payload = json.loads(response.read().decode())
    if not payload:
        return None
    return float(payload[0]["lat"]), float(payload[0]["lon"])
