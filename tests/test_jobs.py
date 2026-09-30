from datetime import datetime

from robin.capabilities.jobs import Jobs
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.schedule import run_due
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key


class StubNer(UnavailableNer):
    def available(self) -> bool:
        return True


class Scripted:
    def __init__(self, turns: list[ModelTurn] | None = None) -> None:
        self.turns = list(turns or [ModelTurn("Ready.")])
        self.user = ""
        self.tools: list[str] = []
        self.calls = 0

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.calls += 1
        self.user = str(messages)
        self.tools = [tool["name"] for tool in tools]
        return self.turns.pop(0)


def _jobs(moment: datetime) -> tuple[Assistant, Jobs]:
    jobs = Jobs(clock=lambda: moment)
    assistant = Assistant(ner=StubNer())
    assistant.add(jobs)
    return assistant, jobs


def test_a_sentence_saves_a_daily_automation() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    model = Scripted(
        [
            ModelTurn(
                "",
                (
                    ToolCall(
                        "jobs_add",
                        {
                            "instruction": "create a summary of my day and deliver it",
                            "hour": 8,
                            "minute": 0,
                            "days": "every day",
                        },
                    ),
                ),
            ),
            ModelTurn("Ready."),
        ]
    )
    reply = converse(
        assistant,
        Task("ada", "home", "create a summary of my day and deliver it to me every day at 08:00"),
        model,
    )
    assert reply.text == "Ready."
    assert "jobs_add" in model.tools or "jobs_list" in {t["name"] for t in assistant.tools("ada")}
    saved = jobs.records("ada")
    assert saved
    assert saved[0]["when"] == "08:00 every day"
    assert "summary of my day" in saved[0]["instruction"]
    assert jobs.records("bea") == []


def test_ordinary_sentences_do_not_save_an_automation() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    model = Scripted([ModelTurn("Oslo is the capital.")])
    converse(assistant, Task("ada", "home", "What is the capital of Norway?"), model)
    converse(assistant, Task("ada", "home", "I walk every day"), Scripted([ModelTurn("Noted.")]))
    assert jobs.records("ada") == []


def test_list_change_and_cancel_use_tools() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    converse(
        assistant,
        Task("ada", "home", "remind me to take out the trash every evening"),
        Scripted(
            [
                ModelTurn(
                    "",
                    (
                        ToolCall(
                            "jobs_add",
                            {
                                "instruction": "take out the trash",
                                "hour": 18,
                                "minute": 0,
                                "days": "every day",
                            },
                        ),
                    ),
                ),
                ModelTurn("Saved."),
            ]
        ),
    )
    job_id = jobs.records("ada")[0]["id"]
    listed = Scripted(
        [
            ModelTurn("", (ToolCall("jobs_list", {}),)),
            ModelTurn("You have one job."),
        ]
    )
    converse(assistant, Task("ada", "home", "list my jobs"), listed)
    assert "jobs_list" in listed.tools
    converse(
        assistant,
        Task("ada", "home", "change the trash automation to 07:30"),
        Scripted(
            [
                ModelTurn("", (ToolCall("jobs_update", {"id": job_id, "hour": 7, "minute": 30}),)),
                ModelTurn("Updated."),
            ]
        ),
    )
    assert jobs.records("ada")[0]["when"] == "07:30 every day"
    converse(
        assistant,
        Task("ada", "home", "cancel that"),
        Scripted(
            [
                ModelTurn("", (ToolCall("jobs_cancel", {"id": job_id}),)),
                ModelTurn("Cancelled."),
            ]
        ),
    )
    assert jobs.records("ada") == []


def test_a_saved_automation_runs_once_when_its_time_arrives(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    jobs = Jobs(store=store, clock=lambda: datetime(2026, 9, 23, 7, 0))
    assistant = Assistant(store=store, ner=StubNer())
    assistant.add(jobs)
    converse(
        assistant,
        Task("ada", "home", "create a summary of my day and deliver it to me every day at 08:00"),
        Scripted(
            [
                ModelTurn(
                    "",
                    (
                        ToolCall(
                            "jobs_add",
                            {
                                "instruction": "create a summary of my day and deliver it",
                                "hour": 8,
                                "minute": 0,
                                "days": "every day",
                            },
                        ),
                    ),
                ),
                ModelTurn("Saved."),
            ]
        ),
    )
    early = Scripted([ModelTurn("early")])
    assert run_due(assistant, early, now=datetime(2026, 9, 23, 7, 30)) == []
    due = Scripted([ModelTurn("Day summary ready.")])
    replies = run_due(assistant, due, now=datetime(2026, 9, 23, 8, 0))
    assert replies
    assert "Day summary" in jobs.records("ada")[0]["result"]


def test_every_hour_repeats_and_in_15_minutes_runs_once() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 10, 0))
    converse(
        assistant,
        Task("ada", "home", "check the news every hour"),
        Scripted(
            [
                ModelTurn(
                    "",
                    (ToolCall("jobs_add", {"instruction": "check the news", "every_minutes": 60}),),
                ),
                ModelTurn("Saved."),
            ]
        ),
    )
    assert jobs.records("ada")
    assert any("hour" in row["when"] or "every" in row["when"] for row in jobs.records("ada"))
    converse(
        assistant,
        Task("ada", "home", "remind me in 15 minutes"),
        Scripted(
            [
                ModelTurn(
                    "",
                    (ToolCall("jobs_add", {"instruction": "remind me", "in_minutes": 15}),),
                ),
                ModelTurn("Saved."),
            ]
        ),
    )
    assert len(jobs.records("ada")) >= 2


def test_a_job_created_after_its_time_waits_until_the_next_day() -> None:
    assistant, jobs = _jobs(datetime(2026, 9, 23, 18, 0))
    converse(
        assistant,
        Task("ada", "home", "summary every day at 08:00"),
        Scripted(
            [
                ModelTurn(
                    "",
                    (
                        ToolCall(
                            "jobs_add",
                            {
                                "instruction": "summary",
                                "hour": 8,
                                "minute": 0,
                                "days": "every day",
                            },
                        ),
                    ),
                ),
                ModelTurn("Saved."),
            ]
        ),
    )
    row = jobs.records("ada")[0]
    assert "08:00" in row["when"]


def test_jobs_add_at_a_date_and_time_runs_once() -> None:
    now = datetime(2026, 9, 30, 21, 30)
    jobs = Jobs({}, clock=lambda: now)
    saved = jobs.invoke("ada", "jobs_add", {"instruction": "Remind the person: match at 20:45", "at": "2026-10-01T19:45"})
    assert "once at 2026-10-01 19:45" in saved
    assert jobs.due(datetime(2026, 10, 1, 19, 44)) == []
    work = jobs.due(datetime(2026, 10, 1, 19, 45))
    assert len(work) == 1 and "match at 20:45" in work[0].text
    work[0].finish("sent")
    assert jobs.due(datetime(2026, 10, 1, 19, 50)) == []


def test_jobs_add_at_rejects_past_and_garbage() -> None:
    now = datetime(2026, 9, 30, 21, 30)
    jobs = Jobs({}, clock=lambda: now)
    assert "already passed" in jobs.invoke("ada", "jobs_add", {"instruction": "x", "at": "2026-09-30T20:00"})
    assert "Could not read" in jobs.invoke("ada", "jobs_add", {"instruction": "x", "at": "tomorrow"})
    assert jobs.invoke("ada", "jobs_list", {}) == "No automations."


def test_reminder_tools_point_at_jobs_add() -> None:
    from robin.capabilities.bills import Bills
    from robin.capabilities.notify import Notify

    described = {tool.name: tool.description for cap in (Jobs, Notify, Bills) for tool in cap.tools}
    assert "remind" in described["jobs_add"].lower()
    assert "jobs_add" in described["notify_person"]
    assert "jobs_add" in described["bills_remind"]
