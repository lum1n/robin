"""Automations described in ordinary sentences. Each account sees only its own."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from robin.capability import Capability, DueWork, Effect, FieldClass, FieldSpec, Tool, current_task
from robin.store import HouseholdStore

_RECURRENCE = re.compile(
    r"\b(?:every|each)\s+(?:day|morning|evening|night|weekday|weekdays)\b|\bdaily\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    r"\b(?P<prefix>at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<suffix>am|pm)?\b",
    re.IGNORECASE,
)
_LIST = re.compile(
    r"\b(?:list|show|what(?: is| are)?|which)\b.{0,48}\b(?:jobs?|automations?)\b|\b(?:my|all)\s+(?:jobs?|automations?)\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(r"\b(?:cancel|delete|remove)\b", re.IGNORECASE)
_EDIT = re.compile(r"\b(?:edit|change|update|move|reschedule)\b", re.IGNORECASE)
_JOB_WORD = re.compile(r"\b(?:jobs?|automations?|schedule|schedules|reminder|reminders)\b", re.IGNORECASE)
_CUE = re.compile(
    r"\b(?:remind|summary|summarize|deliver|send|notify|alert|check|jobs?|automations?|schedule|scheduled)\b|\btell me\b|\bmessage me\b",
    re.IGNORECASE,
)
_FILLER = re.compile(
    r"\b(?:please|can you|could you|i want you to|i want|set up|setup|create|schedule|an automation(?: to)?|a job(?: to)?|job to|automation to)\b",
    re.IGNORECASE,
)
_SKIP = {"a", "an", "the", "my", "me", "to", "and", "of", "it", "for", "please", "that", "this", "you"}


class Jobs(Capability):
    id = "jobs"
    tools = [
        Tool(
            name="jobs_list",
            description="List this account's automations.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="jobs_add",
            description=(
                "Set a reminder or scheduled task for this account; at the chosen time Robin runs the instruction "
                "and pushes the answer to the person. Use this for any 'remind me …'. "
                "Provide instruction plus one of: at (one time, local date and time such as 2026-10-01T19:45), "
                "in_minutes (one time), hour/minute (daily), or every_minutes. "
                "For a reminder, phrase instruction as the message to deliver, e.g. "
                "'Remind the person: the match starts at 20:45.'"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "instruction": {"type": "string"},
                    "at": {"type": "string", "description": "One-time local date and time, YYYY-MM-DDTHH:MM."},
                    "hour": {"type": "integer"},
                    "minute": {"type": "integer"},
                    "days": {"type": "string"},
                    "every_minutes": {"type": "integer"},
                    "in_minutes": {"type": "integer"},
                },
                "required": ["instruction"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="jobs_update",
            description="Change one of this account's automations.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "instruction": {"type": "string"},
                    "at": {"type": "string", "description": "One-time local date and time, YYYY-MM-DDTHH:MM."},
                    "hour": {"type": "integer"},
                    "minute": {"type": "integer"},
                    "days": {"type": "string"},
                    "every_minutes": {"type": "integer"},
                    "in_minutes": {"type": "integer"},
                },
                "required": ["id"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="jobs_cancel",
            description="Cancel one of this account's automations.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("when", FieldClass.ORDINARY),
        FieldSpec("instruction", FieldClass.ORDINARY, free_text=True),
        FieldSpec("result", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(
        self,
        jobs: dict[str, list[dict]] | None = None,
        *,
        store: HouseholdStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or datetime.now
        self._lock = threading.Lock()
        self._jobs: dict[str, list[dict]] = {}
        if jobs is not None:
            self._jobs = {account_id: [dict(row) for row in rows] for account_id, rows in jobs.items()}
            if store is not None:
                for account_id in self._jobs:
                    self._save(account_id)
            return
        if store is not None:
            self._jobs = {account_id: [dict(row) for row in rows] for account_id, rows in store.load_jobs().items()}

    def status(self, account_id: str) -> str:
        with self._lock:
            count = len(self._jobs.get(account_id, []))
        return f"jobs: {count} automation(s)"

    def brief(self, account_id: str, text: str = "") -> list[str]:
        rows = self.records(account_id)
        if not rows:
            return []
        lines = ["Scheduled jobs:"]
        for row in rows[:12]:
            line = f"- {row['id']} at {row['when']}: {row['instruction']}"
            if row.get("result"):
                line += f" Last: {row['result'][:120]}"
            lines.append(line)
        if len(rows) > 12:
            lines.append(f"- (+{len(rows) - 12} more)")
        return lines

    def records(self, account_id: str) -> list[dict[str, str]]:
        with self._lock:
            rows = [dict(row) for row in self._jobs.get(account_id, [])]
        return [
            {
                "id": str(row["id"]),
                "when": _when(row),
                "instruction": str(row["instruction"]),
                "result": str(row.get("result", "")),
            }
            for row in rows
        ]

    def due(self, now: datetime) -> list[DueWork]:
        with self._lock:
            pending = [
                (account_id, dict(row))
                for account_id, rows in self._jobs.items()
                for row in rows
                if _is_due(row, now)
            ]
        work: list[DueWork] = []
        for account_id, row in pending:
            job_id = str(row["id"])
            moment = now

            def finish(result: str, account_id: str = account_id, job_id: str = job_id, moment: datetime = moment) -> None:
                self._complete(account_id, job_id, moment, result)

            source = str(row.get("conversation_id") or "").strip()
            work.append(
                DueWork(
                    account_id=account_id,
                    conversation_id=source or f"job-{job_id}",
                    text=f"{row['instruction']}\nDo this now and answer with the delivery.",
                    finish=finish,
                )
            )
        return work

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "jobs_list":
            return self._list(account_id)
        if tool_name == "jobs_add":
            instruction = str(arguments.get("instruction", "")).strip()
            schedule = _schedule_from_arguments(arguments, self.clock())
            if isinstance(schedule, str):
                return schedule
            if schedule is None or not instruction:
                return "Say what to do, and a time such as 08:00, every hour, in 15 minutes, or at 2026-10-01T19:45."
            return self._add(account_id, instruction, schedule)
        if tool_name == "jobs_update":
            instruction = str(arguments.get("instruction", "")).strip()
            schedule = _schedule_from_arguments(arguments, self.clock(), partial=True)
            if isinstance(schedule, str):
                return schedule
            return self._update(
                account_id,
                str(arguments.get("id", "")),
                instruction=instruction,
                schedule=schedule,
            )
        if tool_name == "jobs_cancel":
            return self._cancel(account_id, str(arguments.get("id", "")))
        raise NotImplementedError(tool_name)

    def _add(self, account_id: str, instruction: str, schedule: dict) -> str:
        instruction = instruction[:500]
        with self._lock:
            rows = self._jobs.setdefault(account_id, [])
            job_id = _fresh_id(instruction, int(schedule["hour"]), int(schedule["minute"]), rows)
            turn = current_task.get()
            conversation_id = turn.conversation_id if turn is not None else ""
            job = {
                "id": job_id,
                "instruction": instruction,
                "hour": schedule["hour"],
                "minute": schedule["minute"],
                "days": schedule["days"],
                "every_minutes": schedule["every_minutes"],
                "once": schedule["once"],
                "next_run": schedule["next_run"],
                "not_before": schedule["not_before"],
                "last_run": "",
                "result": "",
                "conversation_id": conversation_id,
            }
            rows.append(job)
            self._save(account_id)
            shown = dict(job)
        return _announce("Saved", shown)

    def _update(
        self,
        account_id: str,
        job_id: str,
        *,
        instruction: str = "",
        schedule: dict | None = None,
    ) -> str:
        with self._lock:
            job = _find(self._jobs.get(account_id, []), job_id)
            if job is None:
                return "No automation matched."
            if instruction:
                job["instruction"] = instruction[:500]
            if schedule is not None:
                job.update(schedule)
                job["last_run"] = ""
            self._save(account_id)
            shown = dict(job)
        return _announce("Updated", shown)

    def _cancel(self, account_id: str, job_id: str) -> str:
        with self._lock:
            rows = self._jobs.get(account_id, [])
            job = _find(rows, job_id)
            if job is None:
                return "No automation matched."
            self._jobs[account_id] = [row for row in rows if row is not job]
            self._save(account_id)
        return f"Cancelled automation {job['id']}."

    def _list(self, account_id: str) -> str:
        with self._lock:
            rows = [dict(row) for row in self._jobs.get(account_id, [])]
        if not rows:
            return "No automations."
        lines = []
        for row in rows:
            line = f"{row['id']} at {_when(row)}: {row['instruction']}"
            if row.get("result"):
                line += f" Last result: {str(row['result'])[:400]}"
            lines.append(line)
        return "Automations:\n" + "\n".join(lines)

    def _complete(self, account_id: str, job_id: str, moment: datetime, result: str) -> None:
        with self._lock:
            job = _find(self._jobs.get(account_id, []), job_id)
            if job is None:
                return
            job["result"] = result[:2000]
            every = int(job.get("every_minutes") or 0)
            if job.get("once"):
                job["last_run"] = _stamp(moment)
                job["next_run"] = ""
            elif every:
                job["last_run"] = _stamp(moment)
                job["next_run"] = _advance(str(job.get("next_run", "")), every, moment)
            else:
                job["last_run"] = moment.date().isoformat()
            self._save(account_id)

    def _save(self, account_id: str) -> None:
        if self.store is not None:
            self.store.save_jobs(account_id, self._jobs.get(account_id, []))


_EVERY = re.compile(
    r"\bevery\s+(?:half\s+(?:an?\s+)?)?(?:(\d+)\s+)?(hours?|minutes?|mins?)\b",
    re.IGNORECASE,
)
_DELAY = re.compile(
    r"\bin\s+(?:half\s+(?:an?\s+)?)?(?:(\d+|an|a)\s+)?(hours?|minutes?|mins?)\b",
    re.IGNORECASE,
)


def _wants_automation(task: str) -> bool:
    if _LIST.search(task) or _JOB_WORD.search(task):
        return True
    return _timed(task) and _CUE.search(task) is not None


def _interpret(task: str, jobs: list[dict]) -> str | None:
    if _LIST.search(task) and not _timed(task):
        return "list"
    matched = _targets(task, jobs)
    pronoun = re.search(r"\b(?:it|that|this)\b", task, re.IGNORECASE)
    if _CANCEL.search(task) and (_JOB_WORD.search(task) or matched or (len(jobs) == 1 and pronoun)):
        return "cancel"
    retimed = _clocks(task) or _every_minutes(task) is not None or _delay_minutes(task) is not None
    if _EDIT.search(task) and (_JOB_WORD.search(task) or matched or (len(jobs) == 1 and retimed)):
        return "edit"
    if _timed(task) and _CUE.search(task):
        return "add"
    return None


def _timed(task: str) -> bool:
    return _RECURRENCE.search(task) is not None or _every_minutes(task) is not None or _delay_minutes(task) is not None


def _create_fields(task: str, now: datetime) -> tuple[dict | None, str]:
    delay = _delay_minutes(task)
    every = _every_minutes(task)
    if every is not None:
        return _interval_schedule(every, now), _instruction(task)
    if delay is not None:
        return _once_schedule(now + timedelta(minutes=delay)), _instruction(task)
    clocks = _clocks(task)
    if clocks:
        hour, minute = clocks[0]
    else:
        default = _default_clock(task)
        if default is None:
            return None, _instruction(task)
        hour, minute = default
    return _clock_schedule(hour, minute, _span(task), now, already_ran=False), _instruction(task)


def _edit_fields(task: str, target: dict, jobs: list[dict], now: datetime) -> tuple[dict | None, str]:
    raw = _raw_tail(task)
    focus = raw or task
    delay = _delay_minutes(focus)
    every = _every_minutes(focus)
    if every is not None:
        return _interval_schedule(every, now), ""
    if delay is not None:
        return _once_schedule(now + timedelta(minutes=delay)), ""
    clocks = _clocks(focus)
    instruction = _instruction(raw) if raw else ""
    if clocks:
        hour, minute = clocks[-1]
        same_as_target = (int(target.get("hour", -1)), int(target.get("minute", -1))) == (hour, minute)
        if not (len(clocks) == 1 and same_as_target and _clock_identifies(clocks[0], jobs)):
            days = str(target.get("days") or "daily")
            if re.search(r"\bweekdays?\b", focus, re.IGNORECASE):
                days = "weekdays"
            ran = str(target.get("last_run", "")) == now.date().isoformat()
            return _clock_schedule(hour, minute, days, now, already_ran=ran), ""
    return None, instruction


def _clock_identifies(clock: tuple[int, int], jobs: list[dict]) -> bool:
    return any((int(job["hour"]), int(job["minute"])) == clock for job in jobs)


def _one_target(task: str, jobs: list[dict]) -> dict | None:
    found = _targets(task, jobs)
    if len(found) == 1:
        return found[0]
    if len(jobs) == 1 and (_CANCEL.search(task) or _EDIT.search(task)):
        return jobs[0]
    return None


def _targets(task: str, jobs: list[dict]) -> list[dict]:
    lowered = task.lower()
    found: list[dict] = []
    for job in jobs:
        if str(job["id"]) in lowered:
            found.append(job)
            continue
        tokens = [part for part in str(job["id"]).split("-") if len(part) >= 4]
        if any(re.search(rf"\b{re.escape(part)}\b", lowered) for part in tokens):
            found.append(job)
    if found:
        return found
    clocks = _clocks(task)
    if not clocks:
        return []
    return [job for job in jobs if (int(job["hour"]), int(job["minute"])) == clocks[0]]


def _which(jobs: list[dict], verb: str) -> str:
    if not jobs:
        return "No automations."
    names = ", ".join(f"{job['id']} at {_when(job)}" for job in jobs)
    return f"Which automation should Robin {verb}? {names}"


def _clocks(text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for match in _CLOCK.finditer(text):
        parsed = _parse_clock(match)
        if parsed is not None:
            found.append(parsed)
    return found


def _parse_clock(match: re.Match[str]) -> tuple[int, int] | None:
    if match.group("prefix") is None and match.group("minute") is None and match.group("suffix") is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    suffix = (match.group("suffix") or "").lower()
    if suffix == "pm" and 1 <= hour <= 11:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif suffix == "am" and hour > 12:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _default_clock(text: str) -> tuple[int, int] | None:
    lowered = text.lower()
    if re.search(r"\b(?:every|each)\s+morning\b", lowered):
        return 8, 0
    if re.search(r"\b(?:every|each)\s+evening\b", lowered):
        return 18, 0
    if re.search(r"\b(?:every|each)\s+night\b", lowered):
        return 21, 0
    return None


def _span(text: str) -> str:
    if re.search(r"\bweekdays?\b", text, re.IGNORECASE):
        return "weekdays"
    return "daily"


def _instruction(text: str) -> str:
    cleaned = _RECURRENCE.sub(" ", text)
    cleaned = _EVERY.sub(" ", cleaned)
    cleaned = _DELAY.sub(" ", cleaned)
    spans = [match.span() for match in _CLOCK.finditer(cleaned) if _parse_clock(match) is not None]
    for start, end in reversed(spans):
        cleaned = f"{cleaned[:start]} {cleaned[end:]}"
    cleaned = _FILLER.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:-")
    return cleaned


def _raw_tail(text: str) -> str:
    parts = re.split(r"\bto\b", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 1:
        return ""
    return parts[1].strip()


def _when(job: dict) -> str:
    every = int(job.get("every_minutes") or 0)
    if every:
        return _every_phrase(every)
    if job.get("once"):
        stamp = str(job.get("next_run") or job.get("last_run") or "")
        if job.get("last_run") and not job.get("next_run"):
            return f"once, ran {_stamp_label(str(job['last_run']))}"
        return f"once at {_stamp_label(stamp)}"
    clock = f"{int(job['hour']):02d}:{int(job['minute']):02d}"
    if job.get("days") == "weekdays":
        return f"{clock} on weekdays"
    return f"{clock} every day"


def _announce(verb: str, job: dict) -> str:
    when = _when(job)
    if job.get("every_minutes"):
        return f"{verb} automation {job['id']} {when}, starting {_stamp_label(str(job.get('next_run', '')))}. {job['instruction']}"
    if job.get("once") and job.get("next_run"):
        return f"{verb} automation {job['id']} once at {_stamp_label(str(job['next_run']))}. {job['instruction']}"
    if job.get("once"):
        return f"{verb} automation {job['id']} {when}. {job['instruction']}"
    return f"{verb} automation {job['id']} at {when}, starting {job['not_before']}. {job['instruction']}"


def _every_phrase(minutes: int) -> str:
    if minutes == 60:
        return "every hour"
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"every {minutes} {unit}"
    if minutes % 60 == 0:
        hours = minutes // 60
        unit = "hour" if hours == 1 else "hours"
        return f"every {hours} {unit}"
    return f"every {minutes} minutes"


def _interval_schedule(minutes: int, now: datetime) -> dict:
    nxt = _next_boundary(now, minutes)
    return {
        "hour": nxt.hour,
        "minute": nxt.minute,
        "days": "daily",
        "every_minutes": minutes,
        "once": False,
        "next_run": _stamp(nxt),
        "not_before": nxt.date().isoformat(),
    }


def _once_schedule(when: datetime) -> dict:
    return {
        "hour": when.hour,
        "minute": when.minute,
        "days": "daily",
        "every_minutes": 0,
        "once": True,
        "next_run": _stamp(when),
        "not_before": when.date().isoformat(),
    }


def _clock_schedule(hour: int, minute: int, days: str, now: datetime, *, already_ran: bool) -> dict:
    return {
        "hour": hour,
        "minute": minute,
        "days": days,
        "every_minutes": 0,
        "once": False,
        "next_run": "",
        "not_before": _first_date(hour, minute, days, now, already_ran=already_ran),
    }


def _schedule_from_arguments(arguments: dict, now: datetime, *, partial: bool = False) -> dict | str | None:
    """A schedule, None when no time was given, or a message saying why the time was rejected."""
    at = str(arguments.get("at") or "").strip()
    if at:
        moment = _parse_at(at, now)
        if moment is None:
            return f"Could not read the time {at!r}. Use local YYYY-MM-DDTHH:MM."
        if moment <= now:
            return f"{_stamp(moment)} has already passed (it is now {_stamp(now)})."
        if moment - now > timedelta(days=366):
            return "That is more than a year away."
        return _once_schedule(moment)
    every = _optional_number(arguments.get("every_minutes"), 7 * 24 * 60)
    delay = _optional_number(arguments.get("in_minutes"), 7 * 24 * 60)
    if every:
        return _interval_schedule(every, now)
    if delay:
        return _once_schedule(now + timedelta(minutes=delay))
    hour = _optional_number(arguments.get("hour"), 23)
    minute = _optional_number(arguments.get("minute"), 59)
    if hour is None and minute is None:
        return None if partial else None
    if hour is None or minute is None:
        return None
    days = _days(arguments.get("days", "daily"))
    if days is None:
        return None
    return _clock_schedule(hour, minute, days, now, already_ran=False)


def _parse_at(text: str, now: datetime) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        local = now.tzinfo or datetime.now().astimezone().tzinfo
        moment = moment.astimezone(local)
        if now.tzinfo is None:
            moment = moment.replace(tzinfo=None)
    elif now.tzinfo is not None:
        moment = moment.replace(tzinfo=now.tzinfo)
    return moment.replace(second=0, microsecond=0)


def _every_minutes(text: str) -> int | None:
    match = _EVERY.search(text)
    if match is None:
        return None
    if match.group(0).lower().startswith("every half"):
        return 30
    count = int(match.group(1) or 1)
    return _as_minutes(count, match.group(2))


def _delay_minutes(text: str) -> int | None:
    match = _DELAY.search(text)
    if match is None:
        return None
    if match.group(0).lower().startswith("in half"):
        return 30
    raw = (match.group(1) or "1").lower()
    count = 1 if raw in {"a", "an"} else int(raw)
    return _as_minutes(count, match.group(2))


def _as_minutes(count: int, unit: str) -> int | None:
    if count < 1:
        return None
    minutes = count * 60 if unit.lower().startswith("hour") else count
    if minutes > 7 * 24 * 60:
        return None
    return minutes


def _next_boundary(now: datetime, minutes: int) -> datetime:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((now - start).total_seconds() // 60)
    if elapsed % minutes == 0 and now.second == 0 and now.microsecond == 0:
        step = elapsed + minutes
    else:
        step = (elapsed // minutes + 1) * minutes
    return start + timedelta(minutes=step)


def _advance(previous: str, minutes: int, now: datetime) -> str:
    nxt = _parse_stamp(previous) or now.replace(second=0, microsecond=0)
    nxt += timedelta(minutes=minutes)
    while nxt <= now:
        nxt += timedelta(minutes=minutes)
    return _stamp(nxt)


def _stamp(moment: datetime) -> str:
    return moment.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _stamp_label(stamp: str) -> str:
    parsed = _parse_stamp(stamp)
    if parsed is None:
        return stamp
    if parsed.date() != datetime.now(parsed.tzinfo).date():
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.strftime("%H:%M")


def _parse_stamp(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _first_date(hour: int, minute: int, days: str, now: datetime, *, already_ran: bool) -> str:
    day = now.date()
    if already_ran or not _on_day(day, days) or _at_or_after(hour, minute, now):
        day += timedelta(days=1)
        while not _on_day(day, days):
            day += timedelta(days=1)
    return day.isoformat()


def _is_due(job: dict, now: datetime) -> bool:
    every = int(job.get("every_minutes") or 0)
    if job.get("once") or every:
        if job.get("once") and job.get("last_run"):
            return False
        nxt = _parse_stamp(str(job.get("next_run", "")))
        return nxt is not None and now >= nxt
    if now.date().isoformat() < str(job.get("not_before", "")):
        return False
    if job.get("last_run") == now.date().isoformat():
        return False
    if not _on_day(now.date(), str(job.get("days", "daily"))):
        return False
    return _at_or_after(int(job["hour"]), int(job["minute"]), now)


def _on_day(day, days: str) -> bool:
    if days == "weekdays":
        return day.weekday() < 5
    return True


def _at_or_after(hour: int, minute: int, now: datetime) -> bool:
    return (now.hour, now.minute) >= (hour, minute)


def _fresh_id(instruction: str, hour: int, minute: int, rows: list[dict]) -> str:
    words = [word for word in re.findall(r"[a-z0-9]+", instruction.lower()) if word not in _SKIP]
    base = "-".join(words[:4])[:32].strip("-") or f"daily-{hour:02d}{minute:02d}"
    taken = {str(row["id"]) for row in rows}
    if base not in taken:
        return base
    number = 2
    while f"{base}-{number}" in taken:
        number += 1
    return f"{base}-{number}"


def _find(rows: list[dict], job_id: str) -> dict | None:
    wanted = job_id.strip().lower()
    for row in rows:
        if str(row["id"]) == wanted:
            return row
    return None


def _number(value: object, limit: int) -> int | None:
    text = str(value).strip()
    if not text.isdigit():
        return None
    number = int(text)
    if 0 <= number <= limit:
        return number
    return None


def _optional_number(value: object, limit: int) -> int | None:
    if value is None or value == "":
        return None
    return _number(value, limit)


def _days(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"daily", "day", "every day"}:
        return "daily"
    if text in {"weekdays", "weekday"}:
        return "weekdays"
    return None
