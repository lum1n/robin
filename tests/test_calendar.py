import json

from robin.capabilities.calendar import Calendar, CalDAV, calendar_secret
from robin.loop import converse
from robin.model import ModelTurn
from robin.policy import Task
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
    assert ada == [{"id": "1", "title": "Oncologist Tuesday", "when": "tomorrow"}]
    assert bea == [{"id": "1", "title": "bea-only-meeting", "when": "friday"}]
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
    held = assistant.invoke("ada", "agenda", "calendar_add", {"title": f"visit {SECRET}", "when": "tomorrow"})
    assert held["status"] == "confirm"
    assert directory.puts == []
    done = assistant.invoke(
        "ada",
        "agenda",
        "calendar_add",
        {"title": f"visit {SECRET}", "when": "tomorrow"},
        confirmed=True,
    )
    assert done["status"] == "done"
    assert done["result"].startswith("added")
    assert directory.puts[0][0] == "https://cal.example/ada"
    assert SECRET in directory.puts[0][3]
    assert PASSWORD not in directory.puts[0][3]
    log = json.dumps(assistant.activity.read("ada"))
    assert PASSWORD not in log
    assert SECRET not in log
    folded = CalDAV(assistant.broker, fetch=directory.fetch, put=directory.put)
    folded_body = "BEGIN:VEVENT\nSUMMARY:Oncologist \n Tuesday\nDTSTART:tomorrow\nEND:VEVENT\n"
    directory.boxes["ada@example.com"] = folded_body
    assert folded.events("ada") == [{"id": "1", "title": "Oncologist Tuesday", "when": "tomorrow"}]


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


def test_asking_for_the_calendar_uses_the_tool() -> None:
    from robin.model import ToolCall
    from robin.ner import UnavailableNer

    class ReadyNer(UnavailableNer):
        def available(self) -> bool:
            return True

    assistant = Assistant(ner=ReadyNer())
    directory = Directory()
    assistant.broker.put("ada", "calendar", calendar_secret(url="https://cal.example/ada", user="ada@example.com", password=PASSWORD))
    assistant.add(Calendar(CalDAV(assistant.broker, fetch=directory.fetch, put=directory.put)))

    class Scripted:
        def __init__(self) -> None:
            self.turns = [
                ModelTurn("", (ToolCall("calendar_list", {}),)),
                ModelTurn("You have Oncologist Tuesday."),
            ]

        def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
            return self.turns.pop(0)

    reply = converse(assistant, Task("ada", "home", "what is on my calendar"), Scripted())
    assert "Oncologist" in reply.text
    assert PASSWORD not in reply.text
    assert directory.fetches


def test_hello_does_not_fetch_the_calendar() -> None:
    fetches: list[str] = []

    def fetch(url: str, user: str, password: str) -> str:
        fetches.append(url)
        return "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Dentist\nDTSTART:tomorrow\nEND:VEVENT\nEND:VCALENDAR\n"

    assistant = Assistant()
    assistant.broker.put(
        "ada",
        "calendar",
        calendar_secret(url="https://cal.example/ada", user="ada@example.com", password=PASSWORD),
    )
    assistant.add(Calendar(CalDAV(assistant.broker, fetch=fetch, put=lambda *args: None)))
    assistant.decide(Task("ada", "home", "hello"))
    assert fetches == []
    converse(assistant, Task("ada", "home", "hello"), _Plain("hi"))
    assert fetches == []


def test_calendar_tools_stay_available_alongside_the_browser() -> None:
    assistant = Assistant()
    calendar = Calendar(CalDAV(assistant.broker))
    assistant.add(calendar)
    names = {tool["name"] for tool in assistant.tools("ada")}
    assert "calendar_list" in names
    assert calendar.status("ada") == "calendar: not connected, connect it in the app"


def test_calendar_tools_do_not_depend_on_request_text() -> None:
    calendar = Calendar(CalDAV(Assistant().broker))
    names = {tool.name for tool in calendar.available_tools("ada")}
    assert names == {tool.name for tool in calendar.tools}


class _Plain:
    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, *, messages, tools):
        return ModelTurn(self.text)
