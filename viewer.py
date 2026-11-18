"""Hello-world observability pages for the factory.

Serves three pages. The home page, at ``/``, says hello and shows how many runs
the factory has recorded, plus the outcome and reason of the most recent run.
The runs page, at ``/runs``, lists every run, newest first. The run page, at
``/runs/<id>``, shows one run and its steps. Uses only the Python standard
library.

There are three layers: data (``read_*``), presentation (``render_*``) and HTTP
(``_Handler``). The data layer hands the presentation layer one ``PageData``,
``RunsPageData`` or ``RunPageData``; the presentation layer never reads the
database or the clock.
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


class LatestRun(NamedTuple):
    """What the page knows about the most recent run."""

    outcome: str  # "" until the run has ended
    reason: str  # "" until the run has ended


class Unreadable(Enum):
    """Marks a fact that could not be read from the database."""

    UNREADABLE = "unreadable"


UNREADABLE = Unreadable.UNREADABLE


class PageData(NamedTuple):
    """Everything the page shows: what the data layer hands to the presentation layer."""

    count: int | Unreadable  # number of rows in ``runs``
    latest: LatestRun | None | Unreadable  # None only when ``runs`` has no rows


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
        return self.outcome != ""

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


class RunDetail(NamedTuple):
    """What the run page knows about its run: the summary plus the rest."""

    summary: RunSummary
    target: str
    branch: str  # "" when the run has none
    reason: str  # "" when the run has none
    attempt_limit: int


class RunPageData(NamedTuple):
    """Everything the run page shows: the data layer's hand-off to presentation."""

    run_id: int  # the id asked for
    run: RunDetail | None | Unreadable  # None only when ``runs`` has no such row
    steps: tuple[StepSummary, ...] | Unreadable  # in the order to show them
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


def _parse_checks(value) -> tuple[Check, ...] | Unreadable:
    """Return the checks a stored ``steps.checks`` value names; ``()`` for NULL."""
    if value is None:
        return ()
    if not isinstance(value, str):
        return UNREADABLE
    try:
        items = json.loads(value)
    except (ValueError, RecursionError):
        return UNREADABLE
    if not isinstance(items, list):
        return UNREADABLE
    checks = []
    for item in items:
        if not isinstance(item, dict):
            return UNREADABLE
        name = item.get("name")
        passed = item.get("passed")
        if not isinstance(name, str) or not isinstance(passed, bool):
            return UNREADABLE
        checks.append(Check(name, passed))
    return tuple(checks)


def read_page_data() -> PageData:
    """Read everything the page shows, over one read-only connection.

    The database is opened read-only, so it is never created or changed. Both facts
    are read inside one read transaction, so they describe the same moment. A fact
    that could not be read is ``UNREADABLE``; ``latest`` is None only when the
    ``runs`` table was read and has no rows.
    """
    try:
        conn = _connect()
    except sqlite3.Error:
        return PageData(UNREADABLE, UNREADABLE)
    try:
        try:
            conn.execute("BEGIN")
        except sqlite3.Error:
            return PageData(UNREADABLE, UNREADABLE)

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

        return PageData(count, latest)
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
    """Read one run and its steps, lowest step id first, over one read-only connection.

    Both are read inside one read transaction, so they describe the same moment.
    If the run could not be read, ``run`` and ``steps`` are both ``UNREADABLE``.
    ``run`` is None only when the ``runs`` table was read and has no row with that
    id; the steps are then not read and ``steps`` is empty. If only the steps could
    not be read, the run is kept and ``steps`` is ``UNREADABLE``.
    """
    read_at = datetime.now(timezone.utc)
    try:
        conn = _connect()
    except sqlite3.Error:
        return RunPageData(run_id, UNREADABLE, UNREADABLE, read_at)
    try:
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT id, state, outcome, request, created_at, ended_at, "
                "target, branch, reason, attempt_limit FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        except sqlite3.Error:
            return RunPageData(run_id, UNREADABLE, UNREADABLE, read_at)
        if row is None:
            return RunPageData(run_id, None, (), read_at)
        run = RunDetail(
            RunSummary(
                row[0],
                row[1] or "",
                row[2] or "",
                row[3] or "",
                _parse_time(row[4]),
                _parse_time(row[5]),
            ),
            row[6] or "",
            row[7] or "",
            row[8] or "",
            row[9],
        )

        try:
            step_rows = conn.execute(
                "SELECT id, name, persona, attempt, started_at, ended_at, passed, "
                "checks FROM steps WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        except sqlite3.Error:
            return RunPageData(run_id, run, UNREADABLE, read_at)
    finally:
        conn.close()
    steps = tuple(
        StepSummary(
            step[0],
            step[1] or "",
            step[2] or "",
            step[3],
            _parse_time(step[4]),
            _parse_time(step[5]),
            None if step[6] is None else bool(step[6]),
            _parse_checks(step[7]),
        )
        for step in step_rows
    )
    return RunPageData(run_id, run, steps, read_at)


def _document(title: str, body: str) -> str:
    """Return a full HTML document; ``body`` is lines each ending in a newline."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
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
    if latest.outcome == "":
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


def render_page(data: PageData) -> str:
    """Return the full HTML page for the given page data."""
    count, latest = data
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
    return (
        f'<tr id="run-{run.id}">'
        f'<td class="run-id"><a href="/runs/{run.id}">{run.id}</a></td>'
        f'<td class="run-status">{html.escape(run.status)}</td>'
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


def _render_step_row(step: StepSummary, read_at: datetime) -> str:
    """Return the table line for one step, ending in a newline."""
    span = step.duration(read_at)
    duration = _format_duration(span)
    if span is not None and not step.finished:
        duration += " so far"
    if step.passed is None:
        result = "not finished"
    else:
        result = "passed" if step.passed else "failed"
    if step.checks is UNREADABLE:
        checks = "unreadable"
    else:
        checks = ", ".join(_render_check(check) for check in step.checks)
    return (
        f'<tr id="step-{step.id}">'
        f'<td class="step-name">{html.escape(step.name)}</td>'
        f'<td class="step-persona">{html.escape(step.persona)}</td>'
        f'<td class="step-attempt">{step.attempt}</td>'
        f'<td class="step-duration">{duration}</td>'
        f'<td class="step-result">{result}</td>'
        f'<td class="step-checks">{checks}</td>'
        "</tr>\n"
    )


def _render_steps(steps: tuple[StepSummary, ...] | Unreadable, read_at: datetime) -> str:
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
        + "".join(_render_step_row(step, read_at) for step in steps)
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
    """Return the full HTML page for one run, its steps in the order given."""
    run_id, run, steps, read_at = data
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
        body = _render_run_facts(run) + _render_steps(steps, read_at)
    return _document(
        f"Run {run_id} - tomfactory viewer", f"<h1>Run {run_id}</h1>\n" + body
    )


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


# Every path served, tried in order against the whole path. Each function takes the
# pattern's groups as strings and returns a Response. Any other path is a 404.
_ROUTES = (
    (re.compile(r"/"), _home_route),
    (re.compile(r"/runs"), _runs_route),
    (re.compile(r"/runs/([1-9][0-9]{0,17})"), _run_route),
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
