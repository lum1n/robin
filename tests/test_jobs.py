from datetime import datetime

from robin.capabilities.jobs import Jobs
from robin.loop import converse
from robin.model import ModelTurn
from robin.policy import Task
from robin.schedule import run_due
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


class Scripted:
    def __init__(self) -> None:
        self.user = ""
        self.tools: list[str] = []
        self.calls = 0

    def complete(self, *, system: str, user: str, tools: list[dict]) -> ModelTurn:
        self.calls += 1
        self.user = user
        self.tools = [tool["name"] for tool in tools]
        return ModelTurn("Ready.")


def _jobs(moment: datetime) -> tuple[Assistant, Jobs]:
    jobs = Jobs(clock=lambda: moment)
    assistant = Assistant()
    assistant.add(jobs)
    return assistant, jobs


def test_a_sentence_saves_a_daily_automation() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    model = Scripted()
    reply = converse(
        assistant,
        Task("ada", "home", "create a summary of my day and deliver it to me every day at 08:00"),
        model,
    )
    assert reply.text == "Ready."
    assert model.tools == []
    assert "08:00" in model.user
    assert "starting 2026-09-24" not in model.user
    assert "[UNRESOLVED]" in model.user
    saved = jobs.records("ada")
    assert saved[0]["id"] == "summary-day-deliver"
    assert saved[0]["when"] == "08:00 every day"
    assert "summary of my day" in saved[0]["instruction"]
    assert jobs.records("bea") == []
    assert "list_jobs" not in assistant.tools("ada", "What is the capital of Norway?")


def test_ordinary_sentences_do_not_save_an_automation() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    model = Scripted()
    converse(assistant, Task("ada", "home", "What is the capital of Norway?"), model)
    converse(assistant, Task("ada", "home", "I walk every day"), model)
    assert jobs.records("ada") == []
    assert model.tools == []


def test_list_change_and_cancel_use_the_sentence() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    converse(
        assistant,
        Task("ada", "home", "remind me to take out the trash every evening"),
        Scripted(),
    )
    listed = Scripted()
    converse(assistant, Task("ada", "home", "list my jobs"), listed)
    assert listed.tools == []
    assert "18:00 every day" in listed.user
    assert "[UNRESOLVED]" in listed.user
    changed = Scripted()
    converse(assistant, Task("ada", "home", "change the trash automation to 07:30"), changed)
    assert jobs.records("ada")[0]["when"] == "07:30 every day"
    assert "07:30" in changed.user
    converse(assistant, Task("ada", "home", "cancel that"), Scripted())
    assert jobs.records("ada") == []
    quiet = Scripted()
    converse(assistant, Task("ada", "home", "what automations do I have"), quiet)
    assert "No automations." not in quiet.user
    assert "[UNRESOLVED]" in quiet.user


def test_a_saved_automation_runs_once_when_its_time_arrives(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    jobs = Jobs(store=store, clock=lambda: datetime(2026, 9, 23, 7, 0))
    assistant = Assistant(store=store)
    assistant.add(jobs)
    converse(
        assistant,
        Task("ada", "home", "create a summary of my day and deliver it to me every day at 08:00"),
        Scripted(),
    )
    early = Scripted()
    assert run_due(assistant, early, now=datetime(2026, 9, 23, 7, 30)) == []
    assert early.calls == 0
    ran = Scripted()
    replies = run_due(assistant, ran, now=datetime(2026, 9, 23, 8, 5))
    assert ran.calls == 1
    assert replies[0].text == "Ready."
    assert "Today" not in replies[0].text
    assert jobs.records("ada")[0]["result"] == "Ready."
    again = Scripted()
    assert run_due(assistant, again, now=datetime(2026, 9, 23, 8, 20)) == []
    assert again.calls == 0
    store.close()

    revived = Jobs(store=HouseholdStore(path, key))
    assert revived.records("ada")[0]["when"] == "08:00 every day"
    assert revived.records("ada")[0]["result"] == "Ready."
    assert revived.records("bea") == []


def test_every_hour_repeats_and_in_15_minutes_runs_once() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 7))
    hourly = Scripted()
    converse(assistant, Task("ada", "home", "check the news every hour"), hourly)
    assert jobs.records("ada")[0]["when"] == "every hour"
    assert jobs._jobs["ada"][0]["next_run"] == "2026-09-23T19:00"
    assert "19:00" not in hourly.user
    assert "[UNRESOLVED]" in hourly.user
    assert run_due(assistant, Scripted(), now=datetime(2026, 9, 23, 18, 40)) == []
    assert run_due(assistant, Scripted(), now=datetime(2026, 9, 23, 19, 1))
    assert jobs._jobs["ada"][0]["next_run"] == "2026-09-23T20:00"
    assert run_due(assistant, Scripted(), now=datetime(2026, 9, 23, 19, 30)) == []

    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 7))
    once = Scripted()
    converse(assistant, Task("ada", "home", "remind me to stretch in 15 minutes"), once)
    assert jobs.records("ada")[0]["when"] == "once at 18:22"
    assert "18:22" not in once.user
    assert "[UNRESOLVED]" in once.user
    assert run_due(assistant, Scripted(), now=datetime(2026, 9, 23, 18, 21)) == []
    ran = Scripted()
    assert run_due(assistant, ran, now=datetime(2026, 9, 23, 18, 22))
    assert ran.calls == 1
    assert run_due(assistant, Scripted(), now=datetime(2026, 9, 23, 19, 0)) == []
    assert jobs.records("ada")[0]["result"] == "Ready."

    quiet, untouched = _jobs(datetime(2026, 9, 23, 18, 7))
    converse(quiet, Task("ada", "home", "the bus leaves in 15 minutes"), Scripted())
    converse(quiet, Task("ada", "home", "tell me the score every 15 minutes"), Scripted())
    assert untouched.records("ada")[0]["when"] == "every 15 minutes"
    assert untouched._jobs["ada"][0]["next_run"] == "2026-09-23T18:15"
    assert len(untouched.records("ada")) == 1


def test_a_job_created_after_its_time_waits_until_the_next_day() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    converse(
        assistant,
        Task("ada", "home", "summarize my calendar every weekday at 9am"),
        Scripted(),
    )
    assert jobs.records("ada")[0]["when"] == "09:00 on weekdays"
    assert jobs._jobs["ada"][0]["not_before"] == "2026-09-24"
    assert run_due(assistant, Scripted(), now=datetime(2026, 9, 23, 18, 10)) == []
    assert run_due(assistant, Scripted(), now=datetime(2026, 9, 26, 9, 5)) == []
    monday = Scripted()
    assert run_due(assistant, monday, now=datetime(2026, 9, 28, 9, 5))
    assert monday.calls == 1
