import json

from robin.capabilities.calendar import Calendar, CalDAV, calendar_secret
from robin.session import Assistant

PASSWORD = "calendar-password-ada"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"
ICS = "BEGIN:VEVENT\nSUMMARY:Oncologist Tuesday\nDTSTART:tomorrow\nEND:VEVENT\n"
BEA = "BEGIN:VEVENT\nSUMMARY:bea-only-meeting\nDTSTART:friday\nEND:VEVENT\n"


class Directory:
    def __init__(self) -> None:
        self.fetches: list[tuple[str, str, str]] = []
        self.puts: list[tuple[str, str, str, str]] = []
        self.boxes = {"ada@example.com": ICS, "bea@example.com": BEA}

    def fetch(self, url: str, user: str, password: str) -> str:
        self.fetches.append((url, user, password))
        return self.boxes.get(user, "")

    def put(self, url: str, user: str, password: str, body: str) -> None:
        self.puts.append((url, user, password, body))


def test_events_stay_on_the_account_and_the_password_stays_in_the_broker() -> None:
    broker = Assistant().broker
    broker.put("ada", "calendar", calendar_secret(url="https://cal.example/ada", user="ada@example.com", password=PASSWORD))
    broker.put("bea", "calendar", calendar_secret(url="https://cal.example/bea", user="bea@example.com", password="bea-calendar-secret"))
    directory = Directory()
    calendar = CalDAV(broker, fetch=directory.fetch, put=directory.put)
    ada = calendar.events("ada")
    bea = calendar.events("bea")
    assert ada == [{"title": "Oncologist Tuesday", "when": "tomorrow"}]
    assert bea == [{"title": "bea-only-meeting", "when": "friday"}]
    assert PASSWORD not in json.dumps(ada)
    assert directory.fetches[0] == ("https://cal.example/ada", "ada@example.com", PASSWORD)
    assert calendar.events("cara") == []
    broker.put("ada", "calendar", "not-json")
    before = len(directory.fetches)
    assert CalDAV(broker, fetch=directory.fetch, put=directory.put).events("ada") == []
    assert len(directory.fetches) == before


def test_add_waits_for_confirm_and_a_folded_title_is_one_line() -> None:
    assistant = Assistant()
    directory = Directory()
    assistant.broker.put(
        "ada",
        "calendar",
        calendar_secret(url="https://cal.example/ada", user="ada@example.com", password=PASSWORD),
    )
    assistant.add(Calendar(CalDAV(assistant.broker, fetch=directory.fetch, put=directory.put)))
    held = assistant.invoke("ada", "agenda", "add_event", {"title": f"visit {SECRET}", "when": "tomorrow"})
    assert held["status"] == "confirm"
    assert directory.puts == []
    done = assistant.invoke(
        "ada",
        "agenda",
        "add_event",
        {"title": f"visit {SECRET}", "when": "tomorrow"},
        confirmed=True,
    )
    assert done["status"] == "done"
    assert done["result"] == "added"
    assert directory.puts[0][0] == "https://cal.example/ada"
    assert SECRET in directory.puts[0][3]
    assert PASSWORD not in directory.puts[0][3]
    log = json.dumps(assistant.activity.read("ada"))
    assert PASSWORD not in log
    assert SECRET not in log
    folded = CalDAV(assistant.broker, fetch=directory.fetch, put=directory.put)
    folded_body = "BEGIN:VEVENT\nSUMMARY:Oncologist \n Tuesday\nDTSTART:tomorrow\nEND:VEVENT\n"
    directory.boxes["ada@example.com"] = folded_body
    assert folded.events("ada") == [{"title": "Oncologist Tuesday", "when": "tomorrow"}]


def test_a_login_failure_does_not_repeat_the_password() -> None:
    broker = Assistant().broker
    broker.put("ada", "calendar", calendar_secret(url="https://cal.example/ada", user="ada@example.com", password=PASSWORD))

    def boom(url: str, user: str, password: str) -> str:
        raise RuntimeError(password)

    calendar = CalDAV(broker, fetch=boom, put=lambda *args: None)
    try:
        calendar.events("ada")
    except RuntimeError as exc:
        assert str(exc) == "calendar login failed"
        assert PASSWORD not in str(exc)
    else:
        raise AssertionError("login should fail")
    broker.put("ada", "calendar", calendar_secret(url="http://cal.example/ada", user="ada@example.com", password=PASSWORD))
    assert CalDAV(broker, fetch=boom, put=lambda *args: None).events("ada") == []
