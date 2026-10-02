import json
import re
from datetime import datetime, timezone

from robin.capabilities.web import Web
from robin.context import ClientContext, context_lines, merge_context, parse_context
from robin.http import Service, dispatch
from robin.loop import _system
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import Vault, new_key

NOW = datetime(2026, 5, 14, 17, 32, tzinfo=timezone.utc)
DEVICE = {
    "timezone": "Europe/Oslo",
    "locale": "nb_NO",
    "device": "iphone",
    "units": "metric",
    "location": {"locality": "Oslo", "country": "Norway", "latitude": 59.913868, "longitude": 10.752245},
}


class StubNer(UnavailableNer):
    def available(self) -> bool:
        return True


def test_device_time_zone_sets_the_date_weekday_and_offset_without_naming_the_zone() -> None:
    lines = "\n".join(context_lines(parse_context(DEVICE, now=NOW), Vault("ada", "home"), now=NOW))
    assert "Thursday 14 May 2026, 19:32 (UTC+02:00)" in lines
    assert "'Today' means 2026-05-14" in lines
    assert "ISO week 20" in lines
    assert "explicit date in search queries" in lines
    assert "device iPhone, language nb_NO, metric units" in lines
    assert "Europe/Oslo" not in lines


def test_location_reaches_the_prompt_only_as_references_that_restore_locally() -> None:
    vault = Vault("ada", "home")
    lines = "\n".join(context_lines(parse_context(DEVICE, now=NOW), vault, now=NOW))
    assert "Oslo" not in lines and "Norway" not in lines and "59.9" not in lines and "10.75" not in lines
    city = re.search(r"city (\[ADDRESS_[0-9a-f]{32}_\d+\])", lines).group(1)
    coordinates = re.search(r"coordinates (\[ADDRESS_[0-9a-f]{32}_\d+\])", lines).group(1)
    assert vault.restore(f"weather {city}", strict=True) == "weather Oslo"
    # Only about 1 km precision is kept, even locally.
    assert vault.restore(coordinates, strict=True) == "59.91,10.75"


def test_without_device_context_the_server_clock_is_used_and_location_is_unknown() -> None:
    lines = "\n".join(context_lines(None, Vault("ada", "home"), now=NOW))
    assert "'Today' means" in lines
    assert "Location: unknown" in lines


def test_malformed_context_fields_are_dropped() -> None:
    assert parse_context("Europe/Oslo") is None
    assert parse_context({"timezone": "Not/AZone", "device": "toaster", "units": "furlongs"}) is None
    assert parse_context({"timezone": "../../etc/passwd"}) is None
    context = parse_context(
        {
            "timezone": "UTC",
            "locale": "x" * 50,
            "location": {
                "locality": "[ADDRESS_" + "0" * 32 + "_1]",
                "region": "Viken\u0000\u202e",
                "country": "N" * 81,
                "latitude": float("nan"),
                "longitude": 10.0,
            },
        }
    )
    assert context.timezone == "UTC"
    assert context.locale == ""
    assert context.location.locality == ""
    assert context.location.region == "Viken"
    assert context.location.country == ""
    assert context.location.latitude is None and context.location.longitude is None
    assert parse_context({"location": {"latitude": True, "longitude": 1}}) is None


def test_a_turn_without_a_location_fix_keeps_the_last_known_location() -> None:
    first = parse_context(DEVICE, now=NOW)
    second = parse_context({"timezone": "UTC"}, now=NOW)
    merged = merge_context(first, second)
    assert merged.timezone == "UTC"
    assert merged.location == first.location
    assert merge_context(None, ClientContext(timezone="UTC")).location is None


def test_the_device_location_drives_tools_but_never_reaches_the_model(tmp_path) -> None:
    fetched: list[str] = []

    def fetch(url: str, headers: dict[str, str] | None = None) -> str:
        fetched.append(url)
        return json.dumps(
            {
                "results": [
                    {
                        "title": "Oslo football results",
                        "url": "https://example.com/results",
                        "content": "Final scores in Oslo, Norway tonight",
                        "publishedDate": "2026-05-14",
                    }
                ]
            }
        )

    class Model:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
            self.seen.append([dict(message) for message in messages])
            if len(self.seen) == 1:
                city = re.search(r"city (\[ADDRESS_[0-9a-f]{32}_\d+\])", messages[0]["content"]).group(1)
                return ModelTurn("", (ToolCall("web_search", {"query": f"football results {city} 2026-05-14"}),))
            return ModelTurn("Here are today's results.")

    model = Model()
    assistant = Assistant(ner=StubNer(), store=HouseholdStore(tmp_path / "house.sqlite", new_key()))
    assistant.add(Web(fetch=fetch, search_url="http://search.test"))
    service = Service(assistant, model)
    service.auth.register("ada", "ada-session-password")
    headers = {"authorization": f"Bearer {service.auth.login('ada', 'ada-session-password')}"}

    status, payload = dispatch(
        service,
        "POST",
        "/v1/messages",
        body={
            "account_id": "ada",
            "conversation_id": "home",
            "text": "today's football results",
            "allow_cloud": True,
            "context": DEVICE,
        },
        headers=headers,
    )

    assert status == 200, payload
    assert "Oslo" in fetched[0] and "2026-05-14" in fetched[0]
    seen = json.dumps(model.seen)
    for raw in ("Oslo", "Norway", "59.91", "10.75", "Europe/Oslo"):
        assert raw not in seen
    assert "2026-05-14" in seen
    assert "date" in json.dumps(model.seen[1][-1])

    prompt = _system(assistant, "ada", vault=assistant.vaults.get("ada", "home"))
    assert "Location: city [ADDRESS_" in prompt
    vault = assistant.vaults.get("ada", "home")
    coordinates = re.search(r"coordinates (\[ADDRESS_[0-9a-f]{32}_\d+\])", prompt).group(1)
    outcome = assistant.invoke("ada", "home", "web_search", {"query": f"weather {coordinates}"})
    assert outcome["status"] == "confirm" and outcome.get("reason") == "egress"
    assert "Location: unknown" in _system(assistant, "bea", vault=assistant.vaults.get("bea", "home"))
