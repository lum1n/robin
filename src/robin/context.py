"""Situational context a device sends with a turn: clock, time zone, device, location.

Time is not sensitive and reaches the model as plain text. Location never does: every place
value becomes a conversation-scoped reference, restored locally inside tool arguments and in
the person-facing reply. Facts derived from location (public holidays, daylight, home or away)
are computed locally and rendered without place names.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robin.vault import Vault

_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:[_-][A-Za-z0-9]{2,8}){0,3}")
_DEVICES = {"iphone": "iPhone", "ipad": "iPad", "mac": "Mac"}
_UNITS = {"metric", "imperial"}
_CURRENCY = re.compile(r"[A-Z]{3}")
_COUNTRY_CODE = re.compile(r"[A-Z]{2}")
_HOLIDAY_LOOKAHEAD_DAYS = 7
_PLACE_CHARS = 80
# About 1 km: enough for "near me", without pinpointing a home.
_COORDINATE_DECIMALS = 2


@dataclass(frozen=True)
class DeviceLocation:
    locality: str = ""
    region: str = ""
    country: str = ""
    latitude: float | None = None
    longitude: float | None = None
    country_code: str = ""


@dataclass(frozen=True)
class ClientContext:
    timezone: str = ""
    locale: str = ""
    device: str = ""
    units: str = ""
    currency: str = ""
    location: DeviceLocation | None = None
    received: datetime | None = None


def parse_context(raw: Any, *, now: datetime | None = None) -> ClientContext | None:
    """Accept only well-formed fields; anything malformed is dropped rather than trusted."""
    if not isinstance(raw, dict):
        return None
    zone = _zone(raw.get("timezone"))
    locale = raw.get("locale")
    locale = locale if isinstance(locale, str) and len(locale) <= 35 and _LOCALE.fullmatch(locale) else ""
    device = raw.get("device")
    device = device.lower() if isinstance(device, str) and device.lower() in _DEVICES else ""
    units = raw.get("units")
    units = units if isinstance(units, str) and units in _UNITS else ""
    currency = raw.get("currency")
    currency = currency if isinstance(currency, str) and _CURRENCY.fullmatch(currency) else ""
    location = _location(raw.get("location"))
    if not (zone or locale or device or units or currency or location):
        return None
    return ClientContext(
        timezone=zone,
        locale=locale,
        device=device,
        units=units,
        currency=currency,
        location=location,
        received=now or datetime.now(timezone.utc),
    )


def merge_context(previous: ClientContext | None, current: ClientContext) -> ClientContext:
    """A turn without a location fix keeps the last known one; other fields follow the device."""
    if current.location is None and previous is not None and previous.location is not None:
        return ClientContext(
            timezone=current.timezone,
            locale=current.locale,
            device=current.device,
            units=current.units,
            currency=current.currency,
            location=previous.location,
            received=current.received,
        )
    return current


def context_lines(
    context: ClientContext | None,
    vault: Vault | None,
    *,
    now: datetime | None = None,
    home: dict[str, str] | None = None,
    style: str = "auto",
) -> list[str]:
    moment = now or datetime.now(timezone.utc)
    zone = ZoneInfo(context.timezone) if context is not None and context.timezone else None
    local = moment.astimezone(zone) if zone is not None else moment.astimezone()
    offset = local.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    lines = [
        "Context (from the person's device; trust it over your training data):",
        f"- Now: {local:%A} {local.day} {local:%B %Y}, {local:%H:%M} ({offset}), ISO week {local.isocalendar().week}. "
        f"'Today' means {local:%Y-%m-%d}.",
        "- For news, scores, results, weather, prices, or opening hours, put the explicit date in search queries "
        "and check that results are dated for it. Never present older results as today's; say if nothing current was found.",
    ]
    location = context.location if context is not None else None
    day = _day_line(local.date(), _country_code(context))
    if day:
        lines.append(day)
    light = _daylight_line(location, moment)
    if light:
        lines.append(light)
    if context is not None:
        device = _DEVICES.get(context.device, "")
        details = [
            part
            for part in (
                f"device {device}" if device else "",
                f"language {context.locale}" if context.locale else "",
                f"{context.units} units" if context.units else "",
                f"currency {context.currency}" if context.currency else "",
            )
            if part
        ]
        if details:
            lines.append(
                f"- {', '.join(details)}. Format numbers, dates, and prices the way that language and currency expect."
            )
    place = _place_references(location, vault)
    if place:
        lines.append(
            f"- Location: {place}. These references are filled in locally when copied into tool arguments "
            "(e.g. transit_trip from=<city or coordinate reference>, web_search 'weather <city reference> <date>'). "
            "When they ask where they are, copy the city or coordinate reference into the reply; it is shown to them. "
            "Never guess a different place. For next departure or nearby, omit from or use these references — "
            "do not invent a city."
        )
    else:
        lines.append(_unknown_location_line(home or {}, vault))
    whereabouts = _whereabouts(location, home or {})
    if whereabouts:
        lines.append(f"- The person is {whereabouts}.")
    reply = _style_line(style, context.device if context is not None else "")
    if reply:
        lines.append(reply)
    return lines


def _unknown_location_line(home: dict[str, str], vault: Vault | None) -> str:
    line = (
        "- Location: unknown (the device shared none). Never assume where the person is — not from their "
        "language, time zone, earlier replies, or your training data."
    )
    city = (home.get("city") or "").strip()
    address = (home.get("address") or "").strip()
    if city and vault is not None:
        return (
            f"{line} Their saved home city is {vault.token('ADDRESS', city)}; for tasks that depend on where they "
            "are now (next departure, nearby, weather here), use it and say you assumed home, or ask."
        )
    if address and vault is not None:
        return (
            f"{line} Their saved address is {vault.token('ADDRESS', address)}; for tasks that depend on where they "
            "are now (next departure, nearby, weather here), use it and say you assumed home, or ask."
        )
    return f"{line} For tasks that depend on where they are now (next departure, nearby, weather here), ask first."


def _country_code(context: ClientContext | None) -> str:
    if context is None:
        return ""
    if context.location is not None and context.location.country_code:
        return context.location.country_code
    parts = re.split(r"[_-]", context.locale)
    return next((part.upper() for part in parts[1:] if len(part) == 2 and part.isalpha()), "")


def _day_line(today: date, country_code: str) -> str:
    kind = "weekend" if today.weekday() >= 5 else "weekday"
    calendar = _holidays(country_code, today.year)
    if calendar is None:
        return f"- Day type: {kind}."
    # Holiday names would reveal the country, so only dates are shown.
    if today in calendar:
        return f"- Day type: {kind}, public holiday today (shops, offices, and services may be closed)."
    for ahead in range(1, _HOLIDAY_LOOKAHEAD_DAYS + 1):
        upcoming = today + timedelta(days=ahead)
        if upcoming in calendar:
            when = "tomorrow" if ahead == 1 else f"{upcoming:%A} {upcoming:%Y-%m-%d}"
            return f"- Day type: {kind}, next public holiday {when}."
    return f"- Day type: {kind}, no public holiday in the next {_HOLIDAY_LOOKAHEAD_DAYS} days."


def _holidays(country_code: str, year: int):
    if not country_code:
        return None
    try:
        import holidays
    except ImportError:
        return None
    try:
        return holidays.country_holidays(country_code, years=(year, year + 1))
    except (NotImplementedError, KeyError, ValueError):
        return None


def _daylight_line(location: DeviceLocation | None, moment: datetime) -> str:
    if location is None or location.latitude is None or location.longitude is None:
        return ""
    events: list[tuple[datetime, str]] = []
    for offset in (-1, 0, 1, 2):
        day = (moment.astimezone(timezone.utc) + timedelta(days=offset)).date()
        events.extend(_sun_events(location.latitude, location.longitude, day))
    events.sort()
    past = [event for event in events if event[0] <= moment]
    upcoming = [event for event in events if event[0] > moment]
    if not past and not upcoming:
        return ""
    state = "daylight" if past and past[-1][1] == "sunrise" else "dark"
    if not past:
        state = "dark" if upcoming[0][1] == "sunrise" else "daylight"
    if not upcoming:
        return f"- Light: {state} now."
    hours = (upcoming[0][0] - moment).total_seconds() / 3600
    when = "within the hour" if hours < 1 else f"in about {round(hours)} h"
    return f"- Light: {state} now; {upcoming[0][1]} {when}."


def _sun_events(latitude: float, longitude: float, day: date) -> list[tuple[datetime, str]]:
    """Sunrise equation (NOAA approximation, within a few minutes); empty in polar day or night."""
    julian_day = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp() / 86400 + 2440587.5
    n = math.ceil(julian_day - 2451545.0 + 0.0008)
    mean_solar = n - longitude / 360
    anomaly = math.radians((357.5291 + 0.98560028 * mean_solar) % 360)
    center = 1.9148 * math.sin(anomaly) + 0.02 * math.sin(2 * anomaly) + 0.0003 * math.sin(3 * anomaly)
    ecliptic = math.radians((math.degrees(anomaly) + center + 180 + 102.9372) % 360)
    transit = 2451545.0 + mean_solar + 0.0053 * math.sin(anomaly) - 0.0069 * math.sin(2 * ecliptic)
    declination = math.asin(math.sin(ecliptic) * math.sin(math.radians(23.4397)))
    phi = math.radians(latitude)
    cos_hour = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(declination)) / (
        math.cos(phi) * math.cos(declination)
    )
    if not -1 <= cos_hour <= 1:
        return []
    hour = math.degrees(math.acos(cos_hour)) / 360

    def moment(julian: float) -> datetime:
        return datetime.fromtimestamp((julian - 2440587.5) * 86400, tz=timezone.utc)

    return [(moment(transit - hour), "sunrise"), (moment(transit + hour), "sunset")]


def _whereabouts(location: DeviceLocation | None, home: dict[str, str]) -> str:
    """Compared locally; neither the home address nor the city reaches the model."""
    if location is None:
        return ""
    home_city = _fold(home.get("city", ""))
    home_country = _fold(home.get("country", ""))
    here_city = _fold(location.locality)
    here_countries = {_fold(location.country), _fold(location.country_code)} - {""}
    if home_city and here_city and home_city == here_city:
        return "in their home city"
    # Device and profile may name the country in different languages; a matching code also counts.
    if home_country and here_countries and home_country not in here_countries:
        return "abroad (outside their home country)"
    if home_city and here_city:
        return "away from their home city"
    if home_country and here_countries:
        return "in their home country"
    return ""


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _style_line(style: str, device: str) -> str:
    if style == "concise":
        guidance = "keep replies short and scannable; offer details instead of giving them"
    elif style == "detailed":
        guidance = "give thorough replies with context and steps"
    elif device == "iphone":
        guidance = "on a phone: keep replies short and scannable; offer to expand"
    elif device in ("ipad", "mac"):
        guidance = "fuller replies with structure are fine"
    else:
        guidance = ""
    language = "Reply in the language the person writes in."
    return f"- Reply style: {guidance}. {language}" if guidance else f"- {language}"


def _place_references(location: DeviceLocation | None, vault: Vault | None) -> str:
    if location is None or vault is None:
        return ""
    parts: list[str] = []
    if location.locality:
        parts.append(f"city {vault.token('ADDRESS', location.locality)}")
    if location.region and location.region not in (location.locality, location.country):
        parts.append(f"region {vault.token('ADDRESS', location.region)}")
    if location.country:
        parts.append(f"country {vault.token('ADDRESS', location.country)}")
    if location.latitude is not None and location.longitude is not None:
        coordinates = f"{location.latitude:.{_COORDINATE_DECIMALS}f},{location.longitude:.{_COORDINATE_DECIMALS}f}"
        parts.append(f"approximate coordinates {vault.token('ADDRESS', coordinates)}")
    return ", ".join(parts)


def _zone(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64 or ".." in value or value.startswith("/"):
        return ""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    return value


def _location(raw: Any) -> DeviceLocation | None:
    if not isinstance(raw, dict):
        return None
    latitude = _coordinate(raw.get("latitude"), 90)
    longitude = _coordinate(raw.get("longitude"), 180)
    if latitude is None or longitude is None:
        latitude = longitude = None
    location = DeviceLocation(
        locality=_place(raw.get("locality")),
        region=_place(raw.get("region")),
        country=_place(raw.get("country")),
        latitude=latitude,
        longitude=longitude,
        country_code=_code(raw.get("country_code")),
    )
    if not (location.locality or location.region or location.country or latitude is not None):
        return None
    return location


def _code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    code = value.upper()
    return code if _COUNTRY_CODE.fullmatch(code) else ""


def _coordinate(value: Any, limit: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > limit:
        return None
    return round(number, _COORDINATE_DECIMALS)


def _place(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(char for char in value if unicodedata.category(char)[0] != "C")
    text = " ".join(text.split())
    # Brackets would let a place name forge a reference.
    if not text or len(text) > _PLACE_CHARS or any(char in text for char in "[]"):
        return ""
    return text
