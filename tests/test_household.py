import json
import re
from datetime import date, datetime, timezone

import pytest

from robin.airlock import redact
from robin.context import _daylight_line, _day_line, context_lines, parse_context
from robin.household import (
    HouseholdError,
    Member,
    age_band,
    member_terms,
    parse_members,
    parse_preferences,
)
from robin.http import Service, dispatch
from robin.loop import _system
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import Vault, new_key

NOW = datetime(2026, 5, 14, 17, 32, tzinfo=timezone.utc)
REF = r"\[PERSON_[0-9a-f]{32}_\d+\]"
MEMBERS = [
    {"relation": "partner", "given_name": "Kari", "family_name": "Nordmann", "birth_year": 1988},
    {"relation": "child", "given_name": "Ola", "birth_year": 2018},
]


class StubNer(UnavailableNer):
    def available(self) -> bool:
        return True


def _service(tmp_path, model=None) -> tuple[Service, dict[str, str]]:
    assistant = Assistant(ner=StubNer(), store=HouseholdStore(tmp_path / "house.sqlite", new_key()))
    service = Service(assistant, model)
    service.auth.register("ada", "ada-session-password")
    service.auth.register("bea", "bea-session-password")
    return service, {"authorization": f"Bearer {service.auth.login('ada', 'ada-session-password')}"}


def test_members_are_validated() -> None:
    assert parse_members(MEMBERS, current_year=2026)[0].full_name == "Kari Nordmann"
    for bad in (
        "Kari",
        [{"relation": "boss", "given_name": "Kari"}],
        [{"relation": "child", "given_name": "K"}],
        [{"relation": "child", "given_name": "[PERSON_x]"}],
        [{"relation": "child", "given_name": "Ola", "birth_year": 1800}],
        [{"relation": "child", "given_name": "Ola", "birth_year": True}],
        [{"relation": "child", "given_name": "Ola"}] * 13,
    ):
        with pytest.raises(HouseholdError):
            parse_members(bad, current_year=2026)


def test_only_a_coarse_age_band_is_derived() -> None:
    assert [age_band(year, 2026) for year in (2023, 2018, 2011, 1988, None)] == ["under 6", "6-12", "13-17", "adult", ""]


def test_case_variants_of_a_household_name_share_one_reference() -> None:
    vault = Vault("ada", "home")
    terms = member_terms(parse_members(MEMBERS, current_year=2026))
    text, _ = redact("ola and OLA and Ola; Kari Nordmann", vault, vocabulary=terms)
    references = re.findall(REF, text)
    assert len(set(references[:3])) == 1
    assert vault.restore(references[0], strict=True) == "Ola"
    assert vault.restore(references[3], strict=True) == "Kari Nordmann"


def test_household_reaches_the_model_only_as_references_and_age_bands(tmp_path) -> None:
    seen: list[str] = []
    calls: list[dict] = []

    class Model:
        def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
            seen.append(json.dumps(messages))
            if len(seen) == 1:
                child = re.search(rf"child ({REF})", messages[0]["content"]).group(1)
                return ModelTurn("", (ToolCall("note", {"text": f"pick up {child}"}),))
            return ModelTurn("Done.")

    service, headers = _service(tmp_path, Model())
    status, payload = dispatch(
        service, "POST", "/v1/household", body={"account_id": "ada", "members": MEMBERS}, headers=headers
    )
    assert status == 200 and payload["members"][1]["given_name"] == "Ola"

    from robin.capability import Capability, Effect, Result, Tool

    class Notes(Capability):
        id = "notes"
        tools = [Tool("note", "Write a note.", {"type": "object"}, Effect.READ)]
        fields = []

        def invoke(self, account_id: str, name: str, arguments: dict) -> Result:
            calls.append(arguments)
            return Result(f"Saved note about Ola for {arguments['text']}")

    service.assistant.add(Notes())
    status, payload = dispatch(
        service,
        "POST",
        "/v1/messages",
        body={"account_id": "ada", "conversation_id": "home", "text": "remind me about ola", "allow_cloud": True},
        headers=headers,
    )
    assert status == 200, payload
    assert calls == [{"text": "pick up Ola"}]
    everything = "\n".join(seen)
    for raw in ("Kari", "Nordmann", "Ola", "1988", "2018"):
        assert raw not in everything
    assert "partner [PERSON_" in seen[0] and "age 6-12" in seen[0]
    # The user's lowercase mention, the prompt, and the tool result all use one reference.
    child = re.search(rf"child ({REF})", seen[0]).group(1)
    assert seen[0].count(child) >= 2 and child in seen[1]


def test_household_is_per_account_and_out_of_overview(tmp_path) -> None:
    service, headers = _service(tmp_path)
    dispatch(service, "POST", "/v1/household", body={"account_id": "ada", "members": MEMBERS}, headers=headers)
    assert dispatch(service, "GET", "/v1/household", query={"account_id": "bea"}, headers=headers)[0] == 401
    status, payload = dispatch(service, "GET", "/v1/household", query={"account_id": "ada"}, headers=headers)
    assert status == 200 and len(payload["members"]) == 2
    assert "Kari" not in json.dumps(service.assistant.overview("ada"))
    assert "Household" not in _system(service.assistant, "bea", vault=service.assistant.vaults.get("bea", "home"))
    bad = dispatch(
        service, "POST", "/v1/household", body={"account_id": "ada", "members": [{"relation": "x"}]}, headers=headers
    )
    assert bad[0] == 400


def test_preferences_validate_and_render_sources_and_style(tmp_path) -> None:
    with pytest.raises(HouseholdError):
        parse_preferences({"sources": [{"topic": "news", "host": "not a host"}]})
    with pytest.raises(HouseholdError):
        parse_preferences({"style": "shouty"})
    parsed = parse_preferences({"sources": [{"topic": "news", "host": "https://www.NRK.no/"}]})
    assert parsed.sources[0].host == "nrk.no"

    service, headers = _service(tmp_path)
    service.assistant.set_household("ada", (Member("child", "Ola"),))
    body = {
        "account_id": "ada",
        "preferences": {"style": "concise", "sources": [{"topic": "Ola school", "host": "skole.no"}]},
    }
    assert dispatch(service, "POST", "/v1/preferences", body=body, headers=headers)[0] == 200
    prompt = _system(service.assistant, "ada", vault=service.assistant.vaults.get("ada", "home"))
    assert re.search(rf"Preferred sources: {REF} school -> skole.no", prompt)
    assert "Ola" not in prompt
    assert "Reply style: keep replies short" in prompt


def test_day_type_marks_weekends_and_public_holidays_without_names() -> None:
    assert _day_line(date(2026, 5, 17), "NO") == (
        "- Day type: weekend, public holiday today (shops, offices, and services may be closed)."
    )
    assert "next public holiday Thursday 2026-05-14" in _day_line(date(2026, 5, 12), "NO")
    assert "no public holiday" in _day_line(date(2026, 6, 10), "NO")
    assert _day_line(date(2026, 5, 14), "") == "- Day type: weekday."
    assert _day_line(date(2026, 5, 14), "ZZ") == "- Day type: weekday."


def test_daylight_is_coarse_and_handles_polar_summer() -> None:
    from robin.context import DeviceLocation

    oslo = DeviceLocation(latitude=59.91, longitude=10.75)
    # Oslo sunset on 14 May is about 21:30 local (19:30 UTC).
    assert _daylight_line(oslo, NOW) == "- Light: daylight now; sunset in about 2 h."
    assert _daylight_line(oslo, datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc)).startswith("- Light: dark now; sunrise")
    tromso = DeviceLocation(latitude=69.65, longitude=18.96)
    assert _daylight_line(tromso, datetime(2026, 6, 21, 12, tzinfo=timezone.utc)) == ""


def test_home_away_and_currency_are_derived_locally() -> None:
    device = {
        "timezone": "Europe/Oslo",
        "locale": "nb_NO",
        "device": "mac",
        "currency": "NOK",
        "location": {"locality": "Bergen", "country": "Norge", "country_code": "no"},
    }
    context = parse_context(device, now=NOW)
    assert context.currency == "NOK" and context.location.country_code == "NO"
    home = {"city": "Oslo", "country": "Norge", "address": "Storgata 1"}
    lines = "\n".join(context_lines(context, Vault("ada", "home"), now=NOW, home=home))
    assert "away from their home city" in lines
    assert "currency NOK" in lines and "fuller replies" in lines
    assert "Storgata" not in lines and "Bergen" not in lines
    at_home = "\n".join(context_lines(context, Vault("ada", "home"), now=NOW, home={"city": "bergen"}))
    assert "in their home city" in at_home
    abroad = "\n".join(context_lines(context, Vault("ada", "home"), now=NOW, home={"city": "Oslo", "country": "Sverige"}))
    assert "abroad" in abroad
    assert parse_context({"currency": "nok"}) is None
