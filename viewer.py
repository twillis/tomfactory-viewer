"""Hello-world observability pages for the factory.

Serves four pages. The home page, at ``/``, says hello and shows how many runs
the factory has recorded, plus the outcome and reason of the most recent run;
if a run has not ended it shows that run first: its id, the first words of its
request and the step it is on now. The runs page, at ``/runs``, lists every
run, newest first, and marks the one that has not ended. The run page, at
``/runs/<id>``, shows one run, its steps and its latest activity, the 20 newest
events of the run; until the run has ended it also says which step the run is on
now and reloads itself every 5 seconds. The step page, at
``/runs/<id>/steps/<step id>``, shows one step in full: what it was asked, what
it produced, its checks and its events. Uses only the Python standard library.

There are three layers: data (``read_*``), presentation (``render_*``) and HTTP
(``_Handler``). The data layer hands the presentation layer one ``PageData``,
``RunsPageData``, ``RunPageData`` or ``StepPageData``; the presentation layer
never reads the database or the clock.
"""

import argparse
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple


def db_path() -> Path:
    """Return the path of the factory database, read from the environment now."""
    home = os.environ.get("TOMFACTORY_HOME")
    if not home:
        return Path("~/.tomfactory").expanduser() / "factory.db"
    return Path(home) / "factory.db"


# The one rule for whether a run has ended: it has an outcome. Stored, no outcome is
# NULL or ''; the data layer reads both as "". Change the rule here, in both forms.
_NOT_ENDED_SQL = "outcome IS NULL OR outcome = ''"  # a WHERE condition on ``runs``


def _ended(outcome: str) -> bool:
    """Whether a run with this outcome, as read, has ended."""
    return outcome != ""


class LatestRun(NamedTuple):
    """What the page knows about the most recent run."""

    outcome: str  # "" until the run has ended
    reason: str  # "" until the run has ended

    @property
    def ended(self) -> bool:
        """Whether the run has ended: it has an outcome."""
        return _ended(self.outcome)


class Unreadable(Enum):
    """Marks a fact that could not be read from the database."""

    UNREADABLE = "unreadable"


UNREADABLE = Unreadable.UNREADABLE


class RunSummary(NamedTuple):
    """What the runs page knows about one run, and the rules that follow from it."""

    id: int
    state: str
    outcome: str  # "" until the run has ended
    request: str  # the full text
    created_at: datetime | None  # UTC-aware; None if it could not be parsed
    ended_at: datetime | None  # UTC-aware; None if absent or not parseable

    @property
    def ended(self) -> bool:
        """Whether the run has ended: it has an outcome."""
        return _ended(self.outcome)

    @property
    def status(self) -> str:
        """The outcome once the run has ended, its state until then."""
        return self.outcome if self.ended else self.state

    def duration(self, now: datetime) -> timedelta | None:
        """How long the run took, or has been running as of ``now``; None if unknown."""
        if self.created_at is None:
            return None
        if self.ended:
            if self.ended_at is None:
                return None
            span = self.ended_at - self.created_at
        else:
            span = now - self.created_at
        return max(span, timedelta(0))


class RunsPageData(NamedTuple):
    """Everything the runs page shows: the data layer's hand-off to presentation."""

    runs: tuple[RunSummary, ...] | Unreadable  # in the order to show them
    read_at: datetime  # UTC-aware; when the read was made


class Check(NamedTuple):
    """One check the factory ran on a step."""

    name: str
    passed: bool
    note: str = ""  # why it failed, or its output; often "" when it passed


class StepSummary(NamedTuple):
    """What the run page knows about one step, and the rules that follow from it."""

    id: int
    name: str
    persona: str  # "" when the step has none
    attempt: int
    started_at: datetime | None  # UTC-aware; None if it could not be parsed
    ended_at: datetime | None  # UTC-aware; None if absent or not parseable
    passed: bool | None  # None until the step finishes
    checks: tuple[Check, ...] | Unreadable  # in the order they ran; () if none stored

    @property
    def finished(self) -> bool:
        """Whether the step has finished: it has a result."""
        return self.passed is not None

    def duration(self, now: datetime) -> timedelta | None:
        """How long the step took, or has been running as of ``now``; None if unknown."""
        if self.started_at is None:
            return None
        if self.finished:
            if self.ended_at is None:
                return None
            span = self.ended_at - self.started_at
        else:
            span = now - self.started_at
        return max(span, timedelta(0))


def _step_now(newest: StepSummary | None) -> StepSummary | None:
    """The step a run that has not ended is on now: its newest step, if unfinished."""
    return None if newest is None or newest.finished else newest


class OpenRun(NamedTuple):
    """What the home page knows about the run that has not ended."""

    id: int
    request: str  # the full text
    step: StepSummary | None  # the step it is on now; None if none, or steps unreadable


class PageData(NamedTuple):
    """Everything the page shows: what the data layer hands to the presentation layer."""

    count: int | Unreadable  # number of rows in ``runs``
    latest: LatestRun | None | Unreadable  # None only when ``runs`` has no rows
    open_run: OpenRun | None | Unreadable = None  # None only when no run is without an outcome


class RunDetail(NamedTuple):
    """What the run page knows about its run: the summary plus the rest."""

    summary: RunSummary
    target: str
    branch: str  # "" when the run has none
    reason: str  # "" when the run has none
    attempt_limit: int


class Event(NamedTuple):
    """One thing the factory recorded happening during a run."""

    id: int
    at: datetime | None  # UTC-aware; None if it could not be parsed
    kind: str
    summary: str


LATEST_EVENTS = 20  # how many of a run's newest events the run page shows


class RunPageData(NamedTuple):
    """Everything the run page shows: the data layer's hand-off to presentation."""

    run_id: int  # the id asked for
    run: RunDetail | None | Unreadable  # None only when ``runs`` has no such row
    steps: tuple[StepSummary, ...] | Unreadable  # in the order to show them
    events: tuple[Event, ...] | Unreadable  # the newest LATEST_EVENTS of the run, newest first
    read_at: datetime  # UTC-aware; when the read was made

    @property
    def live(self) -> bool:
        """Whether the run was read and has not ended."""
        return isinstance(self.run, RunDetail) and not self.run.summary.ended

    @property
    def current_step(self) -> StepSummary | None:
        """The step a live run is on now: its newest step, if that has not finished."""
        if not self.live or self.steps is UNREADABLE or not self.steps:
            return None
        return _step_now(self.steps[-1])


class StepDetail(NamedTuple):
    """What the step page knows about its step: the summary plus the rest."""

    summary: StepSummary
    asked: str
    produced: dict | Unreadable  # keys in stored order; {} if nothing is stored


class StepPageData(NamedTuple):
    """Everything the step page shows: the data layer's hand-off to presentation."""

    run_id: int  # the run id asked for
    step_id: int  # the step id asked for
    step: StepDetail | None | Unreadable  # None only when that run has no such step
    events: tuple[Event, ...] | Unreadable  # in the order to show them
    read_at: datetime  # UTC-aware; when the read was made


def _connect() -> sqlite3.Connection:
    """Open the factory database read-only, so it is never created or changed."""
    return sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)


# The forms a stored timestamp takes: whole seconds, or with a fraction of a second.
_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_time(value) -> datetime | None:
    """Return the UTC-aware time a stored timestamp names, or None if it names none."""
    if not isinstance(value, str):
        return None
    for time_format in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(value, time_format)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc)
    return None


def _parse_json(value, kind: type):
    """Return the ``kind`` a stored JSON column holds: ``kind()`` for NULL.

    ``UNREADABLE`` if the value is not text, not JSON, or JSON of another kind.
    """
    if value is None:
        return kind()
    if not isinstance(value, str):
        return UNREADABLE
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return UNREADABLE
    return parsed if isinstance(parsed, kind) else UNREADABLE


def _parse_checks(value) -> tuple[Check, ...] | Unreadable:
    """Return the checks a stored ``steps.checks`` value names; ``()`` for NULL."""
    items = _parse_json(value, list)
    if items is UNREADABLE:
        return UNREADABLE
    checks = []
    for item in items:
        if not isinstance(item, dict):
            return UNREADABLE
        name = item.get("name")
        passed = item.get("passed")
        note = item.get("note")
        if not isinstance(name, str) or not isinstance(passed, bool):
            return UNREADABLE
        checks.append(Check(name, passed, note if isinstance(note, str) else ""))
    return tuple(checks)


# The columns of ``steps`` that make a ``StepSummary``, in the order it takes them.
_STEP_COLUMNS = "id, name, persona, attempt, started_at, ended_at, passed, checks"


def _step_summary(row) -> StepSummary:
    """Return the summary of a ``steps`` row that starts with ``_STEP_COLUMNS``."""
    return StepSummary(
        row[0],
        row[1] or "",
        row[2] or "",
        row[3],
        _parse_time(row[4]),
        _parse_time(row[5]),
        None if row[6] is None else bool(row[6]),
        _parse_checks(row[7]),
    )


# The columns of ``events`` that make an ``Event``, in the order it takes them.
_EVENT_COLUMNS = "id, at, kind, summary"


def _event(row) -> Event:
    """Return the event of an ``events`` row that starts with ``_EVENT_COLUMNS``."""
    return Event(row[0], _parse_time(row[1]), row[2] or "", row[3] or "")


def _read_one_and_its_rows(one_sql: str, params: tuple, *rows_sql: str) -> tuple:
    """Read one row, then each set of rows under it, over one read-only connection.

    Every statement takes ``params`` and runs inside one read transaction, so they
    describe the same moment. Returns ``(row, rows, ...)``, one ``rows`` for each
    of ``rows_sql``, in order: all ``UNREADABLE`` if the one row could not be read;
    ``(None, (), ...)`` if there is no such row, no rows statement then being run.
    Otherwise each rows statement is tried on its own: one that could not be read
    is ``UNREADABLE`` for itself only.
    """
    unreadable = (UNREADABLE,) * (1 + len(rows_sql))
    try:
        conn = _connect()
    except sqlite3.Error:
        return unreadable
    try:
        try:
            conn.execute("BEGIN")
            row = conn.execute(one_sql, params).fetchone()
        except sqlite3.Error:
            return unreadable
        if row is None:
            return (None,) + ((),) * len(rows_sql)
        result = [row]
        for sql in rows_sql:
            try:
                result.append(conn.execute(sql, params).fetchall())
            except sqlite3.Error:
                result.append(UNREADABLE)
        return tuple(result)
    finally:
        conn.close()


def _read_step_now(conn: sqlite3.Connection, run_id: int) -> StepSummary | None:
    """Read the step a run is on now; None if there is none or the steps are unreadable."""
    try:
        row = conn.execute(
            f"SELECT {_STEP_COLUMNS} FROM steps WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _step_now(None if row is None else _step_summary(row))


def read_page_data() -> PageData:
    """Read everything the page shows, over one read-only connection.

    The database is opened read-only, so it is never created or changed. All three
    facts are read inside one read transaction, so they describe the same moment. A
    fact that could not be read is ``UNREADABLE``; ``latest`` is None only when the
    ``runs`` table was read and has no rows, ``open_run`` only when it was read and
    every run has an outcome. The open run's step is None if its steps could not
    be read, as on the run page.
    """
    try:
        conn = _connect()
    except sqlite3.Error:
        return PageData(UNREADABLE, UNREADABLE, UNREADABLE)
    try:
        try:
            conn.execute("BEGIN")
        except sqlite3.Error:
            return PageData(UNREADABLE, UNREADABLE, UNREADABLE)

        count: int | Unreadable
        try:
            count = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        except sqlite3.Error:
            count = UNREADABLE

        latest: LatestRun | None | Unreadable
        try:
            row = conn.execute(
                "SELECT outcome, reason FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            latest = None if row is None else LatestRun(row[0] or "", row[1] or "")
        except sqlite3.Error:
            latest = UNREADABLE

        open_run: OpenRun | None | Unreadable
        try:
            row = conn.execute(
                f"SELECT id, request FROM runs WHERE {_NOT_ENDED_SQL} "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            open_run = UNREADABLE
        else:
            open_run = None if row is None else OpenRun(
                row[0], row[1] or "", _read_step_now(conn, row[0])
            )

        return PageData(count, latest, open_run)
    finally:
        conn.close()


def read_runs_page_data() -> RunsPageData:
    """Read every run, highest id first, over one read-only connection.

    One statement is run, so the rows describe one moment. If the database, the
    ``runs`` table or any column the page needs could not be read, ``runs`` is
    ``UNREADABLE``; an empty table gives an empty tuple.
    """
    read_at = datetime.now(timezone.utc)
    try:
        conn = _connect()
    except sqlite3.Error:
        return RunsPageData(UNREADABLE, read_at)
    try:
        rows = conn.execute(
            "SELECT id, state, outcome, request, created_at, ended_at "
            "FROM runs ORDER BY id DESC"
        ).fetchall()
    except sqlite3.Error:
        return RunsPageData(UNREADABLE, read_at)
    finally:
        conn.close()
    runs = tuple(
        RunSummary(
            row[0],
            row[1] or "",
            row[2] or "",
            row[3] or "",
            _parse_time(row[4]),
            _parse_time(row[5]),
        )
        for row in rows
    )
    return RunsPageData(runs, read_at)


def read_run_page_data(run_id: int) -> RunPageData:
    """Read one run, its steps, lowest id first, and its newest events, highest id first.

    All are read over one read-only connection inside one read transaction, so
    they describe the same moment. If the run could not be read, ``run``, ``steps``
    and ``events`` are all ``UNREADABLE``. ``run`` is None only when the ``runs``
    table was read and has no row with that id; the steps and events are then not
    read and both are empty. Steps that could not be read are ``UNREADABLE`` and
    keep the run and the events; events that could not be read are ``UNREADABLE``
    and keep the run and the steps. At most ``LATEST_EVENTS`` events are read.
    """
    read_at = datetime.now(timezone.utc)
    run, steps, events = _read_one_and_its_rows(
        "SELECT id, state, outcome, request, created_at, ended_at, "
        "target, branch, reason, attempt_limit FROM runs WHERE id = ?",
        (run_id,),
        f"SELECT {_STEP_COLUMNS} FROM steps WHERE run_id = ? ORDER BY id",
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE run_id = ? "
        f"ORDER BY id DESC LIMIT {LATEST_EVENTS}",
    )
    if run is not None and run is not UNREADABLE:
        run = RunDetail(
            RunSummary(
                run[0],
                run[1] or "",
                run[2] or "",
                run[3] or "",
                _parse_time(run[4]),
                _parse_time(run[5]),
            ),
            run[6] or "",
            run[7] or "",
            run[8] or "",
            run[9],
        )
    if steps is not UNREADABLE:
        steps = tuple(_step_summary(step) for step in steps)
    if events is not UNREADABLE:
        events = tuple(_event(event) for event in events)
    return RunPageData(run_id, run, steps, events, read_at)


def read_step_page_data(run_id: int, step_id: int) -> StepPageData:
    """Read one step of one run and its events, lowest event id first.

    Both are read over one read-only connection inside one read transaction, so
    they describe the same moment. If the step could not be read, ``step`` and
    ``events`` are both ``UNREADABLE``. ``step`` is None only when the ``steps``
    table was read and has no row with that id for that run; the events are then
    not read and ``events`` is empty. If only the events could not be read, the
    step is kept and ``events`` is ``UNREADABLE``.
    """
    read_at = datetime.now(timezone.utc)
    step, events = _read_one_and_its_rows(
        f"SELECT {_STEP_COLUMNS}, asked, produced FROM steps "
        "WHERE id = ? AND run_id = ?",
        (step_id, run_id),
        f"SELECT {_EVENT_COLUMNS} FROM events "
        "WHERE step_id = ? AND run_id = ? ORDER BY id",
    )
    if step is not None and step is not UNREADABLE:
        step = StepDetail(_step_summary(step), step[8] or "", _parse_json(step[9], dict))
    if events is not UNREADABLE:
        events = tuple(_event(event) for event in events)
    return StepPageData(run_id, step_id, step, events, read_at)


REFRESH_SECONDS = 5  # how often the page of a run that has not ended reloads itself


def _document(title: str, body: str, refresh: int | None = None) -> str:
    """Return a full HTML document; ``body`` is lines each ending in a newline.

    With ``refresh``, the document asks to be reloaded after that many seconds.
    """
    reload = "" if refresh is None else f'<meta http-equiv="refresh" content="{refresh}">\n'
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"{reload}"
        f"<title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{body}"
        "</body>\n"
        "</html>\n"
    )


def _render_latest(latest: LatestRun | None | Unreadable) -> str:
    """Return the page lines for the latest run, each ending in a newline."""
    if latest is UNREADABLE:
        return (
            '<p id="latest-run-unavailable">Latest run unavailable: '
            "could not read outcome and reason from the runs table</p>\n"
        )
    if latest is None:
        return ""
    if not latest.ended:
        return '<p id="latest-run-not-ended">Latest run has not ended yet</p>\n'
    lines = (
        '<p>Latest run outcome: <strong id="latest-run-outcome">'
        f"{html.escape(latest.outcome)}</strong></p>\n"
    )
    if latest.reason != "":
        lines += (
            '<p>Latest run reason: <span id="latest-run-reason">'
            f"{html.escape(latest.reason)}</span></p>\n"
        )
    return lines


def _render_open_run(open_run: OpenRun | None | Unreadable) -> str:
    """Return the page line for the run that has not ended; nothing if there is none."""
    if open_run is None:
        return ""
    if open_run is UNREADABLE:
        return (
            '<p id="open-run-unavailable">Open run unavailable: '
            "could not read the runs table</p>\n"
        )
    if open_run.step is None:
        step = "No step is running now"
    else:
        step = f"Current step: {_step_link(open_run.id, open_run.step)}"
    return (
        f'<p id="open-run">Run <a href="/runs/{open_run.id}">{open_run.id}</a> '
        f"has not ended: {html.escape(_first_words(open_run.request))}. {step}</p>\n"
    )


def render_page(data: PageData) -> str:
    """Return the full HTML page for the given page data."""
    count, latest, open_run = data
    if count is UNREADABLE:
        status = (
            '<p id="run-count-unavailable">Run count unavailable: '
            f"no readable runs table in {html.escape(str(db_path()))}</p>"
        )
    else:
        status = f'<p>Runs recorded: <strong id="run-count">{count}</strong></p>'
    return _document(
        "tomfactory viewer",
        "<h1>Hello from the factory</h1>\n"
        f"{_render_open_run(open_run)}"
        '<p><a id="runs-link" href="/runs">All runs</a></p>\n'
        f"{status}\n"
        f"{_render_latest(latest)}",
    )


REQUEST_WORDS = 10  # how many words of a request the runs page shows


def _first_words(text: str) -> str:
    """Return the first ``REQUEST_WORDS`` words of ``text``, with … if there were more."""
    words = text.split()
    shown = " ".join(words[:REQUEST_WORDS])
    return shown + "…" if len(words) > REQUEST_WORDS else shown


def _format_time(dt: datetime | None) -> str:
    return "unknown" if dt is None else dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_duration(td: timedelta | None) -> str:
    """Return whole seconds as e.g. ``42s``, ``3m 7s``, ``2h 0m 7s`` or ``1d 0h 3m 7s``."""
    if td is None:
        return "unknown"
    minutes, seconds = divmod(max(int(td.total_seconds()), 0), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [(days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s")]
    while len(parts) > 1 and parts[0][0] == 0:
        parts.pop(0)
    return " ".join(f"{amount}{unit}" for amount, unit in parts)


def _render_run_row(run: RunSummary, read_at: datetime) -> str:
    """Return the table line for one run, ending in a newline."""
    span = run.duration(read_at)
    duration = _format_duration(span)
    if span is not None and not run.ended:
        duration += " so far"
    status = html.escape(run.status)
    if not run.ended:
        status = f"<mark>{status}</mark>"  # the run that has not ended stands out
    return (
        f'<tr id="run-{run.id}">'
        f'<td class="run-id"><a href="/runs/{run.id}">{run.id}</a></td>'
        f'<td class="run-status">{status}</td>'
        f'<td class="run-request">{html.escape(_first_words(run.request))}</td>'
        f'<td class="run-created">{_format_time(run.created_at)}</td>'
        f'<td class="run-duration">{duration}</td>'
        "</tr>\n"
    )


def render_runs_page(data: RunsPageData) -> str:
    """Return the full HTML runs page for the given data, rows in the order given."""
    runs, read_at = data
    if runs is UNREADABLE:
        listing = (
            '<p id="runs-unavailable">Runs unavailable: '
            "could not read the runs table</p>\n"
        )
    elif not runs:
        listing = '<p id="no-runs">No runs recorded yet</p>\n'
    else:
        listing = (
            '<table id="runs">\n'
            "<thead>\n"
            "<tr><th>Id</th><th>Status</th><th>Request</th><th>Created</th>"
            "<th>Duration</th></tr>\n"
            "</thead>\n"
            "<tbody>\n"
            + "".join(_render_run_row(run, read_at) for run in runs)
            + "</tbody>\n"
            "</table>\n"
        )
    return _document("Runs - tomfactory viewer", "<h1>Runs</h1>\n" + listing)


def _render_check(check: Check) -> str:
    """Return one check's name, marked if the check failed."""
    name = html.escape(check.name)
    if check.passed:
        return f'<span class="check">{name}</span>'
    return f'<span class="check check-failed">{name} (failed)</span>'


def _step_result(step: StepSummary) -> str:
    """Return ``not finished``, ``passed`` or ``failed``."""
    if step.passed is None:
        return "not finished"
    return "passed" if step.passed else "failed"


def _step_duration(step: StepSummary, read_at: datetime) -> str:
    """Return the step's duration as text, ``so far`` if known and still growing."""
    span = step.duration(read_at)
    duration = _format_duration(span)
    if span is not None and not step.finished:
        duration += " so far"
    return duration


def _step_link(run_id: int, step: StepSummary) -> str:
    """Return the step's name, linked to its page."""
    return f'<a href="/runs/{run_id}/steps/{step.id}">{html.escape(step.name)}</a>'


def _render_current_step(data: RunPageData) -> str:
    """Return the page line saying which step a live run is on; nothing otherwise."""
    if not data.live:
        return ""
    step = data.current_step
    if step is None:
        return '<p id="no-current-step">No step is running now</p>\n'
    return (
        f'<p id="current-step">Current step: {_step_link(data.run_id, step)}, '
        f"{_step_duration(step, data.read_at)}</p>\n"
    )


def _render_step_row(run_id: int, step: StepSummary, read_at: datetime) -> str:
    """Return the table line for one step, ending in a newline."""
    if step.checks is UNREADABLE:
        checks = "unreadable"
    else:
        checks = ", ".join(_render_check(check) for check in step.checks)
    return (
        f'<tr id="step-{step.id}">'
        f'<td class="step-name">{_step_link(run_id, step)}</td>'
        f'<td class="step-persona">{html.escape(step.persona)}</td>'
        f'<td class="step-attempt">{step.attempt}</td>'
        f'<td class="step-duration">{_step_duration(step, read_at)}</td>'
        f'<td class="step-result">{_step_result(step)}</td>'
        f'<td class="step-checks">{checks}</td>'
        "</tr>\n"
    )


def _render_steps(
    run_id: int, steps: tuple[StepSummary, ...] | Unreadable, read_at: datetime
) -> str:
    """Return the page lines for a run's steps, each ending in a newline."""
    if steps is UNREADABLE:
        return (
            '<p id="steps-unavailable">Steps unavailable: '
            "could not read the steps table</p>\n"
        )
    if not steps:
        return '<p id="no-steps">No steps recorded yet</p>\n'
    return (
        '<table id="steps">\n'
        "<thead>\n"
        "<tr><th>Name</th><th>Persona</th><th>Attempt</th><th>Duration</th>"
        "<th>Result</th><th>Checks</th></tr>\n"
        "</thead>\n"
        "<tbody>\n"
        + "".join(_render_step_row(run_id, step, read_at) for step in steps)
        + "</tbody>\n"
        "</table>\n"
    )


def _render_run_facts(run: RunDetail) -> str:
    """Return the page lines describing the run itself, each ending in a newline."""
    summary = run.summary
    label = "Outcome" if summary.ended else "State"
    lines = (
        '<dl id="run">\n'
        f'<dt>Request</dt><dd id="run-request">{html.escape(summary.request)}</dd>\n'
        f'<dt>Target</dt><dd id="run-target">{html.escape(run.target)}</dd>\n'
        f'<dt>Branch</dt><dd id="run-branch">{html.escape(run.branch) or "none"}</dd>\n'
        f'<dt>{label}</dt><dd id="run-status">{html.escape(summary.status)}</dd>\n'
    )
    if summary.ended and run.reason != "":
        lines += f'<dt>Reason</dt><dd id="run-reason">{html.escape(run.reason)}</dd>\n'
    return (
        lines
        + f'<dt>Attempt limit</dt><dd id="run-attempt-limit">{run.attempt_limit}</dd>\n'
        "</dl>\n"
    )


def render_run_page(data: RunPageData) -> str:
    """Return the full HTML page for one run, its steps and events in the order given."""
    run_id, run, steps, events, read_at = data
    if run is None:
        return _document(
            "Run not found - tomfactory viewer",
            "<h1>Run not found</h1>\n"
            f'<p id="run-not-found">No run with id {run_id}</p>\n',
        )
    if run is UNREADABLE:
        body = (
            '<p id="run-unavailable">Run unavailable: '
            "could not read the runs table</p>\n"
        )
    else:
        body = (
            _render_run_facts(run)
            + _render_current_step(data)
            + _render_steps(run_id, steps, read_at)
            + "<h2>Latest activity</h2>\n"
            + _render_events(events)
        )
    return _document(
        f"Run {run_id} - tomfactory viewer",
        f"<h1>Run {run_id}</h1>\n" + body,
        refresh=REFRESH_SECONDS if data.live else None,
    )


def _render_produced(produced: dict | Unreadable) -> str:
    """Return the page lines for what a step produced, each ending in a newline."""
    if produced is UNREADABLE:
        return (
            '<p id="produced-unavailable">Produced unavailable: '
            "could not read what the step produced</p>\n"
        )
    if not produced:
        return '<p id="no-produced">Nothing produced</p>\n'
    lines = '<dl id="step-produced">\n'
    for key, value in produced.items():
        if not isinstance(value, str):
            value = json.dumps(value, indent=2, ensure_ascii=False)
        lines += f"<dt>{html.escape(key)}</dt><dd><pre>{html.escape(value)}</pre></dd>\n"
    return lines + "</dl>\n"


def _render_check_rows(checks: tuple[Check, ...] | Unreadable) -> str:
    """Return the page lines for a step's checks, each ending in a newline."""
    if checks is UNREADABLE:
        return (
            '<p id="checks-unavailable">Checks unavailable: '
            "could not read the checks of this step</p>\n"
        )
    if not checks:
        return '<p id="no-checks">No checks recorded</p>\n'
    return (
        '<table id="checks">\n'
        "<thead>\n"
        "<tr><th>Name</th><th>Result</th><th>Note</th></tr>\n"
        "</thead>\n"
        "<tbody>\n"
        + "".join(
            f'<tr><td class="check-name">{html.escape(check.name)}</td>'
            f'<td class="check-result">{"passed" if check.passed else "failed"}</td>'
            f'<td class="check-note"><pre>{html.escape(check.note)}</pre></td></tr>\n'
            for check in checks
        )
        + "</tbody>\n"
        "</table>\n"
    )


def _render_events(events: tuple[Event, ...] | Unreadable) -> str:
    """Return the page lines for events, each ending in a newline."""
    if events is UNREADABLE:
        return (
            '<p id="events-unavailable">Events unavailable: '
            "could not read the events table</p>\n"
        )
    if not events:
        return '<p id="no-events">No events recorded</p>\n'
    return (
        '<table id="events">\n'
        "<thead>\n"
        "<tr><th>Time</th><th>Kind</th><th>Summary</th></tr>\n"
        "</thead>\n"
        "<tbody>\n"
        + "".join(
            f'<tr id="event-{event.id}">'
            f'<td class="event-time">{_format_time(event.at)}</td>'
            f'<td class="event-kind">{html.escape(event.kind)}</td>'
            f'<td class="event-summary">{html.escape(event.summary)}</td></tr>\n'
            for event in events
        )
        + "</tbody>\n"
        "</table>\n"
    )


def _render_step_facts(step: StepDetail, read_at: datetime) -> str:
    """Return the page lines describing the step itself, each ending in a newline."""
    summary = step.summary
    persona = html.escape(summary.persona) or "none"
    return (
        '<dl id="step">\n'
        f'<dt>Name</dt><dd id="step-name">{html.escape(summary.name)}</dd>\n'
        f'<dt>Persona</dt><dd id="step-persona">{persona}</dd>\n'
        f'<dt>Attempt</dt><dd id="step-attempt">{summary.attempt}</dd>\n'
        f'<dt>Result</dt><dd id="step-result">{_step_result(summary)}</dd>\n'
        "<dt>Duration</dt>"
        f'<dd id="step-duration">{_step_duration(summary, read_at)}</dd>\n'
        "</dl>\n"
        "<h2>Asked</h2>\n"
        f'<pre id="step-asked">{html.escape(step.asked)}</pre>\n'
    )


def render_step_page(data: StepPageData) -> str:
    """Return the full HTML page for one step, its events in the order given."""
    run_id, step_id, step, events, read_at = data
    if step is None:
        return _document(
            "Step not found - tomfactory viewer",
            "<h1>Step not found</h1>\n"
            f'<p id="step-not-found">No step with id {step_id} in run {run_id}</p>\n',
        )
    if step is UNREADABLE:
        body = (
            '<p id="step-unavailable">Step unavailable: '
            "could not read the steps table</p>\n"
        )
    else:
        body = (
            _render_step_facts(step, read_at)
            + "<h2>Produced</h2>\n"
            + _render_produced(step.produced)
            + "<h2>Checks</h2>\n"
            + _render_check_rows(step.summary.checks)
            + "<h2>Events</h2>\n"
            + _render_events(events)
        )
    heading = f"Step {step_id} of run {run_id}"
    return _document(f"{heading} - tomfactory viewer", f"<h1>{heading}</h1>\n" + body)


class Response(NamedTuple):
    """What a route answers with: the HTTP status and the HTML body."""

    status: int
    body: str


def _home_route() -> Response:
    return Response(200, render_page(read_page_data()))


def _runs_route() -> Response:
    return Response(200, render_runs_page(read_runs_page_data()))


def _run_route(run_id: str) -> Response:
    data = read_run_page_data(int(run_id))
    return Response(404 if data.run is None else 200, render_run_page(data))


def _step_route(run_id: str, step_id: str) -> Response:
    data = read_step_page_data(int(run_id), int(step_id))
    return Response(404 if data.step is None else 200, render_step_page(data))


# Every path served, tried in order against the whole path. Each function takes the
# pattern's groups as strings and returns a Response. Any other path is a 404.
_ROUTES = (
    (re.compile(r"/"), _home_route),
    (re.compile(r"/runs"), _runs_route),
    (re.compile(r"/runs/([1-9][0-9]{0,17})"), _run_route),
    (re.compile(r"/runs/([1-9][0-9]{0,17})/steps/([1-9][0-9]{0,17})"), _step_route),
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        for pattern, route in _ROUTES:
            match = pattern.fullmatch(path)
            if match is not None:
                response = route(*match.groups())
                break
        else:
            self.send_error(404)
            return
        body = response.body.encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """Return a bound server that has not started serving."""
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Hello-world factory viewer.")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    args = parser.parse_args(argv)

    server = make_server(port=args.port)
    print(f"Serving on http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
