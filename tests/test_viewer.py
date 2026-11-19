"""Tests for the hello-world observability page (``viewer.py`` at the repository root)."""

import ast
import contextlib
import html
import importlib
import json
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------- helpers: database builders

# The ``runs`` table and its index, exactly as docs/run-store.md gives them. The index
# allows at most one run that has not ended, so tests insert at most one such run.
RUNS_DDL = """
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request         TEXT    NOT NULL,
    target          TEXT    NOT NULL,
    base_commit     TEXT    NOT NULL,
    branch          TEXT,
    workspace       TEXT,
    state           TEXT    NOT NULL CHECK (state IN ('planning','awaiting_approval','working','ended')),
    current_step    TEXT,
    outcome         TEXT    CHECK (outcome IN ('accepted','rejected','needs-me')),
    reason          TEXT,
    ideals          TEXT    NOT NULL DEFAULT '',
    attempt_limit   INTEGER NOT NULL DEFAULT 3,
    driver_pid      INTEGER,
    driver_started  TEXT,
    child_pgid      INTEGER,
    created_at      TEXT    NOT NULL,
    ended_at        TEXT,
    CHECK ((state = 'ended') = (outcome IS NOT NULL))
);
CREATE UNIQUE INDEX one_open_run ON runs ((state != 'ended')) WHERE state != 'ended';
"""

# The ``steps`` table and its index, exactly as docs/run-store.md gives them.
STEPS_DDL = """
CREATE TABLE steps (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    name           TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('agent','code')),
    persona        TEXT,
    attempt        INTEGER NOT NULL,
    asked          TEXT    NOT NULL,
    produced       JSON TEXT,
    checks         JSON TEXT,
    passed         BOOLEAN,
    failure        TEXT    CHECK (failure IN ('timeout','error','guard','malformed','check')),
    started_at     TEXT    NOT NULL,
    ended_at       TEXT,
    tokens         JSON TEXT,
    cost_estimate  REAL
);
CREATE INDEX steps_by_run  ON steps  (run_id, id);
"""

# The ``events`` table, exactly as docs/run-store.md gives it.
EVENTS_DDL = """
CREATE TABLE events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    step_id   INTEGER REFERENCES steps(id),
    at        TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    summary   TEXT    NOT NULL,
    payload   JSON TEXT NOT NULL
);
"""

CREATED ="2026-11-18T21:04:05Z"
ENDED = "2026-11-18T21:07:12Z"  # 3m 7s after CREATED
CREATED_DT = datetime(2026, 11, 18, 21, 4, 5, tzinfo=timezone.utc)
ENDED_DT = CREATED_DT + timedelta(seconds=187)
CREATED_TEXT = "2026-11-18 21:04:05 UTC"

RUN_REQUEST = "Add a page for one run"
RUN_TARGET = "/path/to/target"
RUN_BRANCH = "factory/7-add-a-page"
RUN_REASON = "all checks passed"
TEN_WORDS = "one two three four five six seven eight nine ten"

# Keyword arguments that make a run one that has not ended: for ``insert_detail_run``
# (the stored NULL outcome) and for ``run_summary`` and ``run_detail`` (read as "").
RUNNING = {"state": "working", "outcome": None, "ended_at": None}
NOT_ENDED = {**RUNNING, "outcome": ""}

# Keyword arguments for ``insert_step`` and ``step_summary``: a step not yet finished.
UNFINISHED = {"passed": None, "ended_at": None}


def at(seconds):
    """The time ``seconds`` after ``CREATED_DT``; ``None`` stays ``None``."""
    return None if seconds is None else CREATED_DT + timedelta(seconds=seconds)


def execute_sql(path, sql, params=()):
    """Run one statement with ``params`` or, given none, a whole script; then commit."""
    conn = sqlite3.connect(path)
    try:
        if params:
            conn.execute(sql, params)
        else:
            conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def make_db(home, rows=0, with_runs_table=True, columns=""):
    """Create ``home/factory.db``; optionally with a ``runs`` table holding ``rows`` rows.

    The ``runs`` table made here has an ``id`` plus the raw column SQL in ``columns``.
    """
    home.mkdir(parents=True, exist_ok=True)
    table = f"runs (id INTEGER PRIMARY KEY{columns})" if with_runs_table else "other (x)"
    script = f"CREATE TABLE {table};" + "INSERT INTO runs DEFAULT VALUES;" * rows
    execute_sql(home / "factory.db", script)
    return home / "factory.db"


def checks_json(*checks):
    """The JSON the factory stores for checks given as ``(name, passed)`` pairs."""
    return json.dumps(
        [{"name": n, "passed": p, "note": "", "timed_out": False} for n, p in checks]
    )


def insert_detail_run(
    path, run_id=7, request=RUN_REQUEST, target=RUN_TARGET, branch=RUN_BRANCH,
    state="ended", outcome="accepted", reason=RUN_REASON, attempt_limit=3,
    created_at=CREATED, ended_at=ENDED,
):  # fmt: skip
    """Insert one run, every column the run page reads given; ended by default."""
    execute_sql(
        path,
        "INSERT INTO runs (id, request, target, base_commit, branch, state, outcome, "
        "reason, attempt_limit, created_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, request, target, "abc123", branch, state, outcome, reason)
        + (attempt_limit, created_at, ended_at),
    )


def insert_step(
    path, run_id=7, step_id=None, name="test", kind="agent", persona="tester",
    attempt=1, checks=None, passed=1, started_at=CREATED, ended_at=ENDED,
    asked="do the work", produced=None,
):  # fmt: skip
    """Insert one row into a ``steps`` table; finished and passed by default."""
    execute_sql(
        path,
        "INSERT INTO steps (id, run_id, name, kind, persona, attempt, asked, produced, "
        "checks, passed, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, run_id, name, kind, persona, attempt, asked, produced, checks)
        + (passed, started_at, ended_at),
    )


def insert_event(
    path, run_id=7, step_id=1, event_id=None, at=CREATED, kind="check", summary="ran",
):  # fmt: skip
    """Insert one row into the ``events`` table; of step 1 of run 7 by default."""
    execute_sql(
        path,
        "INSERT INTO events (id, run_id, step_id, at, kind, summary, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, '{}')",
        (event_id, run_id, step_id, at, kind, summary),
    )


def make_run_db(home, runs=(), steps=(), steps_table=True, events=()):
    """Create ``home/factory.db`` with the documented ``runs`` and ``steps`` tables.

    ``runs`` and ``steps`` are sequences of keyword-argument mappings for
    ``insert_detail_run`` and ``insert_step``, inserted in order. With
    ``steps_table=False`` the ``steps`` table is left out. The ``events`` table is
    always made; ``events`` are mappings for ``insert_event``.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "factory.db"
    execute_sql(path, RUNS_DDL + (STEPS_DDL if steps_table else "") + EVENTS_DDL)
    for run in runs:
        insert_detail_run(path, **run)
    for step in steps:
        insert_step(path, **step)
    for event in events:
        insert_event(path, **event)
    return path


# Ways for the whole database to be unreadable. Each takes the home directory.
BREAKS = {
    "database_file_is_missing": lambda home: None,
    "home_directory_is_missing": lambda home: home.rmdir(),
    "database_file_is_empty": lambda home: (home / "factory.db").write_bytes(b""),
    "file_is_not_a_sqlite_database": lambda home: (home / "factory.db").write_bytes(
        b"this is definitely not a sqlite database\n" * 50
    ),
    "factory_db_is_a_directory": lambda home: (home / "factory.db").mkdir(),
    "runs_table_is_missing": lambda home: make_db(home, with_runs_table=False),
}
UNREADABLE_DATABASES = [pytest.param(make, id=name) for name, make in BREAKS.items()]

# Ways for the rows of ``runs`` to be unreadable: the readers that need its columns.
UNREADABLE_ROWS_DATABASES = UNREADABLE_DATABASES + [
    pytest.param(lambda home: make_db(home, rows=3), id="runs_has_only_an_id_column"),
]

# Database states every reader must cope with: how to build each, and how many
# connections to it can be opened at all.
OPENED = {
    "runs_table_is_missing": 1,
    "file_is_not_a_sqlite_database": 1,
    "database_file_is_missing": 0,
    "home_directory_is_missing": 0,
}
DATABASE_STATES = [
    pytest.param(lambda home: make_run_db(home, runs=[{}], steps=[{}]), 1, id="readable"),
    pytest.param(lambda home: make_run_db(home), 1, id="empty_tables"),
] + [pytest.param(BREAKS[name], opened, id=name) for name, opened in OPENED.items()]

# --------------------------------------------------------- helpers: connection watching


class ConnectionProxy:
    """Stands in for the connection the viewer opens.

    It offers only ``execute`` and ``close``, which is all the data layer is meant to
    use on its connection. ``after_execute(sql)`` runs right after each statement.
    """

    def __init__(self, real, after_execute=None):
        self._real = real
        self._after_execute = after_execute
        self.statements = []
        self.close_calls = 0

    def execute(self, sql, *args, **kwargs):
        cursor = self._real.execute(sql, *args, **kwargs)
        self.statements.append(sql)
        if self._after_execute is not None:
            self._after_execute(sql)
        return cursor

    def close(self):
        self.close_calls += 1
        self._real.close()


def wrap_connections(viewer, monkeypatch, after_execute=None):
    """Make ``viewer.sqlite3.connect`` record its calls and return proxies.

    Returns ``(calls, proxies)``: the ``(args, kwargs)`` of every call, recorded even
    when the connect fails, and the proxy made for every connection that opened.
    ``viewer.sqlite3`` is the ``sqlite3`` module itself, so build the database before
    calling this: every later ``sqlite3.connect`` call is recorded and wrapped.
    """
    calls, proxies = [], []
    real_connect = sqlite3.connect

    def wrapping_connect(*args, **kwargs):
        calls.append((args, kwargs))
        proxies.append(ConnectionProxy(real_connect(*args, **kwargs), after_execute))
        return proxies[-1]

    monkeypatch.setattr(viewer.sqlite3, "connect", wrapping_connect)
    return calls, proxies


def assert_one_read_only_connect(calls):
    assert len(calls) == 1
    args, kwargs = calls[0]
    database = args[0] if args else kwargs["database"]
    assert "mode=ro" in str(database)
    assert kwargs.get("uri") is True


def fail_statements_naming(words):
    """An ``after_execute`` hook: a statement containing ``words`` fails."""

    def hook(sql):
        if words in sql.lower():
            raise sqlite3.OperationalError("database is locked")

    return hook


@contextlib.contextmanager
def write_mid_read(
    viewer, monkeypatch, path, schema, follows, insert, seed=lambda path: None
):
    """Commit ``insert`` right after the viewer's first statement that ``follows`` accepts.

    The database is made from ``schema`` in WAL mode, by a connection kept open so it
    stays in that mode with its -wal and -shm files, and then ``seed(path)`` fills it.
    ``follows`` is given each statement in lower case. Yields ``(inserted, proxies)``;
    ``inserted`` ends as ``["attempted", "committed"]`` when the write went through.
    """
    real_connect = sqlite3.connect
    inserted = []

    def insert_after_the_first_match(sql):
        if inserted or not follows(sql.lower()):
            return
        inserted.append("attempted")
        writer = real_connect(path, timeout=2)
        try:
            writer.execute(insert)
            writer.commit()
            inserted.append("committed")
        except sqlite3.Error as error:  # reported by the caller's assertion
            inserted.append(error)
        finally:
            writer.close()

    holder = real_connect(path)
    try:
        mode = holder.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode.lower() == "wal"
        holder.executescript(schema)
        holder.commit()
        seed(path)
        _, proxies = wrap_connections(viewer, monkeypatch, insert_after_the_first_match)
        yield inserted, proxies
    finally:
        holder.close()


# ------------------------------------------------------- helpers: hand-built hand-offs


def facts(data):
    """What a hand-off says was read: every field but the ids asked for and ``read_at``."""
    asked_or_clock = ("run_id", "step_id", "read_at")
    return [v for k, v in data._asdict().items() if k not in asked_or_clock]


def run_summary(
    viewer, run_id=7, state="ended", outcome="accepted", request=RUN_REQUEST,
    created_at=CREATED_DT, ended_at=ENDED_DT,
):  # fmt: skip
    """A hand-built ``RunSummary``; ended and accepted by default (see ``NOT_ENDED``)."""
    return viewer.RunSummary(run_id, state, outcome, request, created_at, ended_at)


def run_detail(
    viewer, target=RUN_TARGET, branch=RUN_BRANCH, reason=RUN_REASON, attempt_limit=3,
    **summary,
):  # fmt: skip
    """A hand-built ``RunDetail`` around ``run_summary(viewer, **summary)``."""
    return viewer.RunDetail(
        run_summary(viewer, **summary), target, branch, reason, attempt_limit
    )


def step_summary(
    viewer, step_id=12, name="test", persona="tester", attempt=1,
    started_at=CREATED_DT, ended_at=ENDED_DT, passed=True, checks=(),
):  # fmt: skip
    """A hand-built ``StepSummary``; finished and passed by default (see ``UNFINISHED``)."""
    return viewer.StepSummary(
        step_id, name, persona, attempt, started_at, ended_at, passed, checks
    )


def passed_step(viewer):
    """Step 11: passed, with one passed check. Rendered as ``PASSED_STEP_ROW``."""
    guard = viewer.Check("guard", True)
    return step_summary(viewer, 11, "red", "test-writer", checks=(guard,))


def failed_step(viewer):
    """Step 12: failed on attempt 2, one check of two failed (``FAILED_STEP_ROW``)."""
    checks = (viewer.Check("guard", True), viewer.Check("tests", False))
    return step_summary(viewer, attempt=2, passed=False, checks=checks)


def render_runs(viewer, runs, read_at=ENDED_DT):
    """Render the /runs page for hand-built runs and a fixed read time."""
    if runs is not viewer.UNREADABLE:
        runs = tuple(runs)
    return viewer.render_runs_page(viewer.RunsPageData(runs, read_at))


def render_run(viewer, run, steps=(), read_at=ENDED_DT, run_id=7):
    """Render the run page for a hand-built run, steps and a fixed read time."""
    if steps is not viewer.UNREADABLE:
        steps = tuple(steps)
    return viewer.render_run_page(viewer.RunPageData(run_id, run, steps, read_at))


def step_detail(viewer, asked="do the work", produced=None, **summary):
    """A hand-built ``StepDetail`` around ``step_summary``; nothing produced by default."""
    produced = {} if produced is None else produced
    return viewer.StepDetail(step_summary(viewer, **summary), asked, produced)


def full_step(viewer):
    """Step 12, failed on attempt 2, and its two events: rendered as ``FULL_BLOCKS``."""
    checks = (viewer.Check("guard", True), viewer.Check("tests", False, "1 failed"))
    produced = {"summary": "added greet", "files": ["café.py"]}
    began = viewer.Event(4, CREATED_DT, "step_start", "began")
    step = step_detail(viewer, "write it", produced, attempt=2, passed=False, checks=checks)
    return step, (began, viewer.Event(3, None, "check", "red"))


def render_step(viewer, step, events=(), read_at=ENDED_DT, run_id=7, step_id=12):
    """Render the step page for a hand-built step, events and a fixed read time."""
    data = viewer.StepPageData(run_id, step_id, step, events, read_at)
    return viewer.render_step_page(data)


# ---------------------------------------------------------------- helpers: expected HTML

RUNS_LINK_LINE = '<p><a id="runs-link" href="/runs">All runs</a></p>\n'
COUNT_LINE = '<p>Runs recorded: <strong id="run-count">{}</strong></p>\n'
COUNT_3 = COUNT_LINE.format(3)
# ``{db}`` is the escaped path of the database the count could not be read from.
COUNT_UNAVAILABLE_LINE = (
    '<p id="run-count-unavailable">Run count unavailable: '
    "no readable runs table in {db}</p>\n"
)
OUTCOME_LINE = '<p>Latest run outcome: <strong id="latest-run-outcome">{}</strong></p>\n'
REASON_LINE = '<p>Latest run reason: <span id="latest-run-reason">{}</span></p>\n'
NOT_ENDED_LINE = '<p id="latest-run-not-ended">Latest run has not ended yet</p>\n'
LATEST_UNAVAILABLE_LINE = (
    '<p id="latest-run-unavailable">Latest run unavailable: '
    "could not read outcome and reason from the runs table</p>\n"
)

NO_RUNS_LINE = '<p id="no-runs">No runs recorded yet</p>\n'
RUNS_UNAVAILABLE_LINE = (
    '<p id="runs-unavailable">Runs unavailable: could not read the runs table</p>\n'
)
RUNS_TABLE_OPEN = (
    '<table id="runs">\n'
    "<thead>\n"
    "<tr><th>Id</th><th>Status</th><th>Request</th><th>Created</th>"
    "<th>Duration</th></tr>\n"
    "</thead>\n"
    "<tbody>\n"
)
TABLE_CLOSE = "</tbody>\n</table>\n"

RUN_UNAVAILABLE_LINE = (
    '<p id="run-unavailable">Run unavailable: could not read the runs table</p>\n'
)
STEPS_UNAVAILABLE_LINE = (
    '<p id="steps-unavailable">Steps unavailable: could not read the steps table</p>\n'
)
NO_STEPS_LINE = '<p id="no-steps">No steps recorded yet</p>\n'
STEPS_TABLE_OPEN = (
    '<table id="steps">\n'
    "<thead>\n"
    "<tr><th>Name</th><th>Persona</th><th>Attempt</th><th>Duration</th>"
    "<th>Result</th><th>Checks</th></tr>\n"
    "</thead>\n"
    "<tbody>\n"
)


def document(title, heading, body):
    """The whole page expected: ``body`` is lines each ending in a newline."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{heading}</h1>\n"
        f"{body}"
        "</body>\n"
        "</html>\n"
    )


def run_row(run_id, status, request, created=CREATED_TEXT, duration="3m 7s"):
    """The table line the /runs page shows for one run; the texts are already escaped."""
    return (
        f'<tr id="run-{run_id}">'
        f'<td class="run-id"><a href="/runs/{run_id}">{run_id}</a></td>'
        f'<td class="run-status">{status}</td>'
        f'<td class="run-request">{request}</td>'
        f'<td class="run-created">{created}</td>'
        f'<td class="run-duration">{duration}</td>'
        "</tr>\n"
    )


def run_not_found_line(run_id):
    return f'<p id="run-not-found">No run with id {run_id}</p>\n'


def run_facts(
    request=RUN_REQUEST, target=RUN_TARGET, branch=RUN_BRANCH, label="Outcome",
    status="accepted", reason=RUN_REASON, attempt_limit=3,
):  # fmt: skip
    """The lines describing the run; texts already escaped. ``reason=None``: no line."""
    reason_line = (
        "" if reason is None else f'<dt>Reason</dt><dd id="run-reason">{reason}</dd>\n'
    )
    return (
        '<dl id="run">\n'
        f'<dt>Request</dt><dd id="run-request">{request}</dd>\n'
        f'<dt>Target</dt><dd id="run-target">{target}</dd>\n'
        f'<dt>Branch</dt><dd id="run-branch">{branch}</dd>\n'
        f'<dt>{label}</dt><dd id="run-status">{status}</dd>\n'
        + reason_line
        + f'<dt>Attempt limit</dt><dd id="run-attempt-limit">{attempt_limit}</dd>\n'
        "</dl>\n"
    )


def step_row(
    step_id, name="test", persona="tester", attempt=1, duration="3m 7s",
    result="passed", checks="", run_id=7,
):  # fmt: skip
    """The table line the run page shows for one step; the texts are already escaped."""
    return (
        f'<tr id="step-{step_id}">'
        f'<td class="step-name"><a href="/runs/{run_id}/steps/{step_id}">{name}</a></td>'
        f'<td class="step-persona">{persona}</td>'
        f'<td class="step-attempt">{attempt}</td>'
        f'<td class="step-duration">{duration}</td>'
        f'<td class="step-result">{result}</td>'
        f'<td class="step-checks">{checks}</td>'
        "</tr>\n"
    )


# The exact rows of ``passed_step`` and ``failed_step``.
PASSED_STEP_ROW = step_row(
    11, "red", "test-writer", 1, "3m 7s", "passed", '<span class="check">guard</span>'
)
FAILED_STEP_ROW = step_row(
    12, "test", "tester", 2, "3m 7s", "failed",
    '<span class="check">guard</span>, '
    '<span class="check check-failed">tests (failed)</span>',
)  # fmt: skip

NO_PRODUCED_LINE = '<p id="no-produced">Nothing produced</p>\n'
NO_CHECKS_LINE = '<p id="no-checks">No checks recorded</p>\n'
NO_EVENTS_LINE = '<p id="no-events">No events recorded</p>\n'
# The three blocks of the step page for ``full_step``: produced keys in stored order
# with a value that is not text as indented JSON, checks and events in the order given.
FULL_BLOCKS = {
    "produced": '<dl id="step-produced">\n'
    "<dt>summary</dt><dd><pre>added greet</pre></dd>\n"
    "<dt>files</dt><dd><pre>[\n  &quot;café.py&quot;\n]</pre></dd>\n"
    "</dl>\n",
    "checks": '<table id="checks">\n<thead>\n'
    "<tr><th>Name</th><th>Result</th><th>Note</th></tr>\n</thead>\n<tbody>\n"
    '<tr><td class="check-name">guard</td><td class="check-result">passed</td>'
    '<td class="check-note"><pre></pre></td></tr>\n'
    '<tr><td class="check-name">tests</td><td class="check-result">failed</td>'
    '<td class="check-note"><pre>1 failed</pre></td></tr>\n' + TABLE_CLOSE,
    "events": '<table id="events">\n<thead>\n'
    "<tr><th>Time</th><th>Kind</th><th>Summary</th></tr>\n</thead>\n<tbody>\n"
    f'<tr id="event-4"><td class="event-time">{CREATED_TEXT}</td>'
    '<td class="event-kind">step_start</td><td class="event-summary">began</td></tr>\n'
    '<tr id="event-3"><td class="event-time">unknown</td>'
    '<td class="event-kind">check</td><td class="event-summary">red</td></tr>\n'
    + TABLE_CLOSE,
}  # fmt: skip
# Formatted with an id prefix, what could not be read and from where; step and run id.
UNAVAILABLE_LINE = '<p id="{}-unavailable">{} unavailable: could not read {}</p>\n'
STEP_NOT_FOUND_LINE = '<p id="step-not-found">No step with id {} in run {}</p>\n'


def step_body(
    name="test", persona="tester", attempt=1, result="passed", duration="3m 7s",
    asked="do the work", produced=NO_PRODUCED_LINE, checks=NO_CHECKS_LINE,
    events=NO_EVENTS_LINE,
):  # fmt: skip
    """The lines the step page shows for a readable step; the texts are already escaped."""
    return (
        '<dl id="step">\n'
        f'<dt>Name</dt><dd id="step-name">{name}</dd>\n'
        f'<dt>Persona</dt><dd id="step-persona">{persona}</dd>\n'
        f'<dt>Attempt</dt><dd id="step-attempt">{attempt}</dd>\n'
        f'<dt>Result</dt><dd id="step-result">{result}</dd>\n'
        f'<dt>Duration</dt><dd id="step-duration">{duration}</dd>\n'
        f'</dl>\n<h2>Asked</h2>\n<pre id="step-asked">{asked}</pre>\n'
        f"<h2>Produced</h2>\n{produced}<h2>Checks</h2>\n{checks}<h2>Events</h2>\n{events}"
    )


# --------------------------------------------------- helpers: fixtures and the server


@pytest.fixture
def viewer():
    """The module under test, imported per test so each test fails on its own."""
    return importlib.import_module("viewer")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point ``TOMFACTORY_HOME`` at an empty temporary directory."""
    monkeypatch.setenv("TOMFACTORY_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def port(viewer, home):
    """Serve the viewer on a free port in a daemon thread; yield the port."""
    server = viewer.make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def fetch(port, path="/", method="GET"):
    """Return ``(status, headers, body_bytes)`` for a request to the test server."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.headers, error.read()
        finally:
            error.close()


class FakeServer:
    def __init__(self):
        self.served = False
        self.closed = False

    def serve_forever(self, *args, **kwargs):
        self.served = True
        raise KeyboardInterrupt

    def shutdown(self):
        pass

    def server_close(self):
        self.closed = True


def run_main_with_fake_server(viewer, monkeypatch, argv):
    """Run ``main(argv)`` against a fake server that is interrupted at once.

    Returns the fake server and the port ``make_server`` was asked for, once.
    """
    fake = FakeServer()
    calls = []
    monkeypatch.setattr(
        viewer, "make_server", lambda *a, **k: calls.append((a, k)) or fake
    )
    try:
        viewer.main(argv)
    except SystemExit as exit_:
        assert exit_.code in (None, 0)
    assert len(calls) == 1
    args, kwargs = calls[0]
    return fake, kwargs["port"] if "port" in kwargs else args[1]


# Each reader of the data layer, as a function of the module under test.
READERS = [
    pytest.param(lambda viewer: viewer.read_page_data(), id="read_page_data"),
    pytest.param(lambda viewer: viewer.read_runs_page_data(), id="read_runs_page_data"),
    pytest.param(lambda viewer: viewer.read_run_page_data(7), id="read_run_page_data"),
    pytest.param(lambda v: v.read_step_page_data(7, 1), id="read_step_page_data"),
]

# ---------------------------------------- db_path, UNREADABLE and the hand-off types


def test_db_path_is_factory_db_inside_tomfactory_home(viewer, home):
    assert viewer.db_path() == home / "factory.db"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_db_path_defaults_to_dot_tomfactory_in_home_when_the_variable_is(
    viewer, tmp_path, monkeypatch, value
):
    if value is None:
        monkeypatch.delenv("TOMFACTORY_HOME", raising=False)
    else:
        monkeypatch.setenv("TOMFACTORY_HOME", value)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert viewer.db_path() == tmp_path / ".tomfactory" / "factory.db"


def test_db_path_reads_the_environment_on_every_call(viewer, tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("TOMFACTORY_HOME", str(first))
    assert viewer.db_path() == first / "factory.db"
    monkeypatch.setenv("TOMFACTORY_HOME", str(second))
    assert viewer.db_path() == second / "factory.db"


def test_unreadable_is_the_single_member_of_the_unreadable_enum(viewer):
    assert viewer.UNREADABLE is viewer.Unreadable.UNREADABLE
    assert list(viewer.Unreadable) == [viewer.UNREADABLE]
    assert isinstance(viewer.UNREADABLE, viewer.Unreadable)


def test_unreadable_is_not_none_and_not_equal_to_a_count_or_a_run(viewer):
    assert viewer.UNREADABLE is not None
    assert viewer.UNREADABLE != 0
    assert viewer.UNREADABLE != ""
    assert viewer.UNREADABLE != viewer.LatestRun("", "")


@pytest.mark.parametrize(
    "name, fields",
    [
        ("PageData", "count latest"),
        ("LatestRun", "outcome reason"),
        ("RunSummary", "id state outcome request created_at ended_at"),
        ("RunsPageData", "runs read_at"),
        ("Check", "name passed note"),
        ("Event", "id at kind summary"),
        ("StepDetail", "summary asked produced"),
        ("StepPageData", "run_id step_id step events read_at"),
        ("StepSummary", "id name persona attempt started_at ended_at passed checks"),
        ("RunDetail", "summary target branch reason attempt_limit"),
        ("RunPageData", "run_id run steps read_at"),
    ],
)
def test_hand_off_type_has_its_fields_in_order(viewer, name, fields):
    assert getattr(viewer, name)._fields == tuple(fields.split())


# ---------------------------------------------------------------------- read_page_data


def test_read_page_data_counts_the_rows_and_takes_the_latest_by_highest_id(viewer, home):
    make_run_db(home, runs=[{"run_id": 50, "reason": "newest by id"}, {"run_id": 7}])
    assert viewer.read_page_data() == viewer.PageData(
        2, viewer.LatestRun("accepted", "newest by id")
    )


def test_read_page_data_latest_is_the_not_ended_row_when_older_rows_have_ended(
    viewer, home
):
    make_run_db(home, runs=[{"run_id": 1}, {"run_id": 2, "reason": None, **RUNNING}])
    assert viewer.read_page_data() == viewer.PageData(2, viewer.LatestRun("", ""))


@pytest.mark.parametrize(
    "outcome, reason, latest",
    [
        pytest.param(None, None, ("", ""), id="both_null"),
        pytest.param("", "", ("", ""), id="both_empty_strings"),
        pytest.param("passed", None, ("passed", ""), id="null_reason"),
        pytest.param(None, "still going", ("", "still going"), id="null_outcome"),
        pytest.param("<b>&", "café ✓ 'q'", ("<b>&", "café ✓ 'q'"), id="text_unchanged"),
    ],
)
def test_read_page_data_latest_holds_the_stored_text_with_empty_strings_for_null(
    viewer, home, outcome, reason, latest
):
    path = make_db(home, columns=", outcome TEXT, reason TEXT")
    execute_sql(path, "INSERT INTO runs (outcome, reason) VALUES (?, ?)", (outcome, reason))
    assert viewer.read_page_data() == viewer.PageData(1, viewer.LatestRun(*latest))


def test_read_page_data_returns_zero_and_none_for_an_empty_runs_table(viewer, home):
    make_run_db(home)
    assert viewer.read_page_data() == viewer.PageData(0, None)
    assert type(viewer.read_page_data().count) is int


def test_read_page_data_keeps_the_count_when_runs_has_no_outcome_or_reason_columns(
    viewer, home
):
    make_db(home, rows=3)
    assert viewer.read_page_data() == viewer.PageData(3, viewer.UNREADABLE)


def test_read_page_data_latest_is_unreadable_for_an_empty_table_without_the_columns(
    viewer, home
):
    # No rows and no columns: "could not read" wins over "no runs" for the latest run,
    # while the count is still a readable zero.
    make_db(home, rows=0)
    data = viewer.read_page_data()
    assert data == viewer.PageData(0, viewer.UNREADABLE)
    assert data.latest is not None


def test_read_page_data_count_and_latest_run_come_from_the_same_moment(
    viewer, home, monkeypatch
):
    schema = (
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, outcome TEXT, reason TEXT);"
        "INSERT INTO runs (outcome, reason) VALUES ('passed', 'first run');"
    )
    insert = "INSERT INTO runs (outcome, reason) VALUES ('failed', 'second run')"
    with write_mid_read(
        viewer, monkeypatch, home / "factory.db", schema, lambda sql: "count" in sql, insert
    ) as (inserted, proxies):
        data = viewer.read_page_data()

        assert inserted == ["attempted", "committed"], (
            "the second run must be written between the count and the latest-run read"
        )
        assert len(proxies) == 1
        assert data == viewer.PageData(1, viewer.LatestRun("passed", "first run"))

        # The second run really was committed: the next read shows it.
        assert viewer.read_page_data() == viewer.PageData(
            2, viewer.LatestRun("failed", "second run")
        )


# ----- every reader: unreadable databases, one read-only connection, no writes, read_at


@pytest.mark.parametrize("read", READERS)
@pytest.mark.parametrize("break_database", UNREADABLE_DATABASES)
def test_every_reader_marks_every_fact_unreadable_when(
    viewer, home, read, break_database
):
    break_database(home)
    assert all(fact is viewer.UNREADABLE for fact in facts(read(viewer)))


@pytest.mark.parametrize("read", READERS)
def test_every_reader_marks_every_fact_unreadable_when_connect_raises(
    viewer, home, monkeypatch, read
):
    make_run_db(home, runs=[{}])

    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(viewer.sqlite3, "connect", failing_connect)
    assert all(fact is viewer.UNREADABLE for fact in facts(read(viewer)))


@pytest.mark.parametrize("read", READERS)
@pytest.mark.parametrize("build, opened", DATABASE_STATES)
def test_every_reader_opens_one_read_only_connection_and_closes_it_once(
    viewer, home, monkeypatch, read, build, opened
):
    build(home)
    calls, proxies = wrap_connections(viewer, monkeypatch)

    read(viewer)

    assert_one_read_only_connect(calls)
    assert [proxy.close_calls for proxy in proxies] == [1] * opened


@pytest.mark.parametrize("read", READERS)
@pytest.mark.parametrize("build, opened", DATABASE_STATES)
def test_every_reader_leaves_every_file_as_it_was(viewer, home, read, build, opened):
    def files():
        if not home.exists():
            return None
        return {p.name: p.read_bytes() for p in home.iterdir()}

    build(home)
    before = files()
    read(viewer)
    read(viewer)
    assert files() == before


@pytest.mark.parametrize("read", READERS)
def test_every_reader_sees_rows_inserted_since_its_last_call(viewer, home, read):
    path = make_run_db(home, runs=[{}])
    before = facts(read(viewer))
    insert_detail_run(path, run_id=8)
    insert_step(path)
    assert facts(read(viewer)) != before


# A statement that fails part-way through a read: the reader, the words naming the
# statements that fail, and the facts the reader must then give.
FAILING_STATEMENTS = [
    pytest.param(
        READERS[1].values[0], "from runs", lambda v: [v.UNREADABLE], id="every_run"
    ),
    pytest.param(
        READERS[2].values[0], "from runs", lambda v: [v.UNREADABLE] * 2, id="the_run"
    ),
    pytest.param(
        READERS[2].values[0], "steps", lambda v: [run_detail(v), v.UNREADABLE],
        id="the_steps_keeping_the_run",
    ),
    pytest.param(READERS[3].values[0], "steps", lambda v: [v.UNREADABLE] * 2, id="the_step"),
    pytest.param(
        READERS[3].values[0], "events", lambda v: [step_detail(v, step_id=1), v.UNREADABLE],
        id="the_events_keeping_the_step",
    ),
]  # fmt: skip


@pytest.mark.parametrize("read, failing, expected", FAILING_STATEMENTS)
def test_a_reader_marks_what_it_could_not_read_unreadable_when_a_statement_fails_on(
    viewer, home, monkeypatch, read, failing, expected
):
    make_run_db(home, runs=[{}], steps=[{}])
    _, proxies = wrap_connections(viewer, monkeypatch, fail_statements_naming(failing))

    assert facts(read(viewer)) == expected(viewer)
    assert [proxy.close_calls for proxy in proxies] == [1]


@pytest.mark.parametrize("read", READERS[1:])
@pytest.mark.parametrize("build, opened", DATABASE_STATES)
def test_read_at_is_the_utc_time_of_the_call_whatever_was_read(
    viewer, home, read, build, opened
):
    build(home)
    before = datetime.now(timezone.utc)
    data = read(viewer)
    after = datetime.now(timezone.utc)
    assert data.read_at.utcoffset() == timedelta(0)
    assert before <= data.read_at <= after


# ------------------------------------- render_page, and rendering without the database

QUOTED = ('say "hi"', "it's \"bad\"")

# What the home page is given, and the lines that follow the link to /runs.
PAGE_BODIES = [
    pytest.param(lambda v: v.PageData(0, None), COUNT_LINE.format(0), id="no_runs"),
    pytest.param(lambda v: v.PageData(3, None), COUNT_3, id="a_count_alone"),
    pytest.param(
        lambda v: v.PageData(3, v.UNREADABLE), COUNT_3 + LATEST_UNAVAILABLE_LINE,
        id="latest_run_unreadable",
    ),
    pytest.param(
        lambda v: v.PageData(v.UNREADABLE, None), COUNT_UNAVAILABLE_LINE,
        id="count_unreadable",
    ),
    pytest.param(
        lambda v: v.PageData(v.UNREADABLE, v.UNREADABLE),
        COUNT_UNAVAILABLE_LINE + LATEST_UNAVAILABLE_LINE,
        id="count_and_latest_run_unreadable",
    ),
    pytest.param(
        lambda v: v.PageData(3, v.LatestRun("failed", "tests red")),
        COUNT_3 + OUTCOME_LINE.format("failed") + REASON_LINE.format("tests red"),
        id="an_ended_run_with_a_reason",
    ),
    pytest.param(
        lambda v: v.PageData(3, v.LatestRun("passed", "")),
        COUNT_3 + OUTCOME_LINE.format("passed"),
        id="an_ended_run_with_an_empty_reason",
    ),
    pytest.param(
        lambda v: v.PageData(3, v.LatestRun("", "still going")),
        COUNT_3 + NOT_ENDED_LINE,
        id="a_run_that_has_not_ended_hides_its_reason",
    ),
    pytest.param(
        lambda v: v.PageData(3, v.LatestRun(*QUOTED)),
        COUNT_3
        + OUTCOME_LINE.format(html.escape(QUOTED[0]))
        + REASON_LINE.format(html.escape(QUOTED[1])),
        id="quotes_in_outcome_and_reason_are_escaped",
    ),
]  # fmt: skip


@pytest.mark.parametrize("data, lines", PAGE_BODIES)
def test_render_page_is_exactly_the_document_with_these_lines_for(
    viewer, home, data, lines
):
    db = html.escape(str(home / "factory.db"))
    assert viewer.render_page(data(viewer)) == document(
        "tomfactory viewer", "Hello from the factory", RUNS_LINK_LINE + lines.format(db=db)
    )


def test_render_page_escapes_html_in_the_database_path(viewer, tmp_path, monkeypatch):
    nasty = tmp_path / "a&b<i>c"
    monkeypatch.setenv("TOMFACTORY_HOME", str(nasty))
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, None))
    assert html.escape(str(nasty / "factory.db")) in page
    assert "<i>" not in page
    assert "a&b" not in page


# Each page rendered from hand-built data: whether it may look up the database path
# (only the home page with an unreadable count names the path), and what it shows.
RENDERS_WITHOUT_THE_DATABASE = [
    pytest.param(
        lambda v: v.render_page(v.PageData(v.UNREADABLE, v.UNREADABLE)), False,
        COUNT_UNAVAILABLE_LINE + LATEST_UNAVAILABLE_LINE,
        id="home_with_nothing_readable",
    ),
    pytest.param(
        lambda v: v.render_page(v.PageData(3, v.LatestRun("failed", "tests red"))), True,
        COUNT_3 + OUTCOME_LINE.format("failed"),
        id="home",
    ),
    pytest.param(
        lambda v: render_runs(v, [run_summary(v, request="given by the caller")]), True,
        run_row(7, "accepted", "given by the caller"),
        id="runs",
    ),
    pytest.param(lambda v: render_runs(v, []), True, NO_RUNS_LINE, id="no_runs"),
    pytest.param(
        lambda v: render_runs(v, v.UNREADABLE), True, RUNS_UNAVAILABLE_LINE,
        id="unreadable_runs",
    ),
    pytest.param(
        lambda v: render_run(
            v, run_detail(v, request="given by the caller"),
            [step_summary(v, name="given-step")],
        ),
        True,
        run_facts(request="given by the caller")
        + STEPS_TABLE_OPEN
        + step_row(12, name="given-step"),
        id="run",
    ),
    pytest.param(
        lambda v: render_run(v, run_detail(v), v.UNREADABLE), True,
        STEPS_UNAVAILABLE_LINE,
        id="unreadable_steps",
    ),
    pytest.param(
        lambda v: render_run(v, v.UNREADABLE, v.UNREADABLE), True, RUN_UNAVAILABLE_LINE,
        id="unreadable_run",
    ),
    pytest.param(
        lambda v: render_run(v, None), True, run_not_found_line(7), id="run_not_found"
    ),
    pytest.param(
        lambda v: render_step(v, step_detail(v, asked="given by the caller")), True,
        '<pre id="step-asked">given by the caller</pre>\n', id="step",
    ),
]  # fmt: skip


@pytest.mark.parametrize("render, forbid_db_path, shown", RENDERS_WITHOUT_THE_DATABASE)
def test_rendering_does_not_open_the_database_or_look_up_its_path(
    viewer, home, monkeypatch, render, forbid_db_path, shown
):
    def forbidden(*args, **kwargs):
        raise AssertionError("rendering must not open the database or look up its path")

    db = html.escape(str(home / "factory.db"))
    monkeypatch.setattr(viewer.sqlite3, "connect", forbidden)
    if forbid_db_path:
        monkeypatch.setattr(viewer, "db_path", forbidden)

    assert shown.format(db=db) in render(viewer)


# ------------------ RunSummary and StepSummary: the rules that follow from what was read

# state, outcome, then what follows: ended, status, and the label the run page gives.
RUN_STATUSES = [
    ("ended", "accepted", True, "accepted", "Outcome"),
    ("ended", "rejected", True, "rejected", "Outcome"),
    ("ended", "needs-me", True, "needs-me", "Outcome"),
    ("planning", "", False, "planning", "State"),
    ("awaiting_approval", "", False, "awaiting_approval", "State"),
    ("working", "", False, "working", "State"),
    pytest.param("working", "rejected", True, "rejected", "Outcome", id="stale_state"),
    pytest.param("ended", "", False, "ended", "State", id="ended_state_and_no_outcome"),
]


@pytest.mark.parametrize("state, outcome, ended, status, label", RUN_STATUSES)
def test_run_summary_ended_and_status_are_decided_by_the_outcome_not_the_state(
    viewer, state, outcome, ended, status, label
):
    run = run_summary(viewer, state=state, outcome=outcome)
    assert run.ended is ended
    assert run.status == status


def test_step_summary_finished_is_decided_by_passed_not_by_ended_at(viewer):
    result_without_end = step_summary(viewer, passed=False, ended_at=None)
    assert result_without_end.finished is True

    end_without_result = step_summary(viewer, passed=None, ended_at=ENDED_DT)
    assert end_without_result.finished is False


# The two types with a duration. Each builder takes whether the thing is done (the
# run has an outcome, the step has a result) and its start and end times.
DURATION_OWNERS = [
    pytest.param(
        lambda viewer, done, start, end: run_summary(
            viewer, outcome="accepted" if done else "", created_at=start, ended_at=end
        ),
        id="RunSummary",
    ),
    pytest.param(
        lambda viewer, done, start, end: step_summary(
            viewer, passed=True if done else None, started_at=start, ended_at=end
        ),
        id="StepSummary",
    ),
]

# done, start, end, now, expected: all in seconds after CREATED_DT, None for absent.
DURATION_CASES = [
    pytest.param(True, 0, 187, 187 + 400 * 86400, 187, id="done_ignores_a_later_now"),
    pytest.param(True, 0, 187, -86400, 187, id="done_ignores_an_earlier_now"),
    pytest.param(False, 0, None, 7207, 7207, id="not_done_is_from_start_to_now"),
    pytest.param(False, 0, 187, 5, 5, id="not_done_ignores_a_stray_end"),
    pytest.param(True, 0.123, 187.623, 187, 187.5, id="keeps_fractions_of_a_second"),
    pytest.param(True, None, 187, 187, None, id="done_without_a_start_is_none"),
    pytest.param(True, 0, None, 187 + 86400, None, id="done_without_an_end_is_none"),
    pytest.param(True, 0, -30, 187, 0, id="done_ending_before_the_start_is_zero"),
    pytest.param(False, 0, None, -30, 0, id="not_done_with_now_before_start_is_zero"),
    pytest.param(False, None, None, 187, None, id="not_done_without_a_start_is_none"),
]


@pytest.mark.parametrize("build", DURATION_OWNERS)
@pytest.mark.parametrize("done, start, end, now, expected", DURATION_CASES)
def test_duration_when(viewer, build, done, start, end, now, expected):
    duration = build(viewer, done, at(start), at(end)).duration(at(now))
    assert duration == (None if expected is None else timedelta(seconds=expected))


# ----------------------------------------------------------------- read_runs_page_data


def test_read_runs_page_data_maps_every_field_of_an_ended_run(viewer, home):
    make_run_db(home, runs=[{"request": "Add a page at /runs", "outcome": "rejected"}])
    assert viewer.read_runs_page_data().runs == (
        viewer.RunSummary(
            7, "ended", "rejected", "Add a page at /runs", CREATED_DT, ENDED_DT
        ),
    )


def test_read_runs_page_data_gives_a_running_run_empty_outcome_and_no_ended_at(
    viewer, home
):
    make_run_db(home, runs=[{"run_id": 4, "request": "Still going", **RUNNING}])
    (run,) = viewer.read_runs_page_data().runs
    assert run == viewer.RunSummary(4, "working", "", "Still going", CREATED_DT, None)
    assert run.outcome == ""
    assert run.ended_at is None


def test_read_runs_page_data_orders_by_id_not_by_created_at(viewer, home):
    make_run_db(
        home,
        runs=[
            {"run_id": 1, "created_at": "2030-01-01T00:00:00Z", "ended_at": None},
            {"run_id": 2, "created_at": "2020-01-01T00:00:00Z", "ended_at": None},
        ],
    )
    assert [run.id for run in viewer.read_runs_page_data().runs] == [2, 1]


def test_read_runs_page_data_gives_empty_strings_and_none_for_null_columns(
    viewer, home
):
    # A looser table than the documented one: every column the page reads is NULL.
    path = make_db(home, with_runs_table=False)
    execute_sql(
        path,
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, state TEXT, outcome TEXT, "
        "request TEXT, created_at TEXT, ended_at TEXT)",
    )
    execute_sql(path, "INSERT INTO runs DEFAULT VALUES")
    assert viewer.read_runs_page_data().runs == (
        viewer.RunSummary(1, "", "", "", None, None),
    )


# Stored values that are not one of the two forms the factory writes.
MALFORMED_TIMES = [
    "yesterday", "", "2026-11-18 21:04:05", "2026-11-18T21:04:05",
    "2026-11-18T21:04:05+00:00", "2026-13-45T99:99:99Z", "2026-11-18", "1763500000",
    b"\x00\x01binary", "2026-11-18T21:04:05.Z", "2026-11-18T21:04:05.1234567Z",
    "2026-11-18T21:04:05.123", "2026-11-18T21:04:05,123Z",
]  # fmt: skip


@pytest.mark.parametrize("stored", MALFORMED_TIMES)
def test_read_runs_page_data_created_at_is_none_when_stored_as(viewer, home, stored):
    make_run_db(home, runs=[{"request": "Odd time", "created_at": stored}])
    data = viewer.read_runs_page_data()
    assert data.runs == (
        viewer.RunSummary(7, "ended", "accepted", "Odd time", None, ENDED_DT),
    )


def test_read_runs_page_data_keeps_other_runs_when_one_has_a_malformed_time(
    viewer, home
):
    make_run_db(home, runs=[{"run_id": 1}, {"run_id": 2, "created_at": "garbage"}])
    runs = viewer.read_runs_page_data().runs
    assert [run.id for run in runs] == [2, 1]
    assert [run.created_at for run in runs] == [None, CREATED_DT]


# The second form the factory stores: the same time with a fraction of a second.
@pytest.mark.parametrize(
    "stored, microsecond",
    [
        ("2026-11-18T21:04:05.250Z", 250000),
        ("2026-11-18T21:04:05.123Z", 123000),
        ("2026-11-18T21:04:05.000Z", 0),
    ],
)
def test_read_runs_page_data_parses_created_at_keeping_the_fraction_of(
    viewer, home, stored, microsecond
):
    make_run_db(home, runs=[{"request": "Fraction", "created_at": stored}])
    (run,) = viewer.read_runs_page_data().runs
    assert run.created_at == CREATED_DT.replace(microsecond=microsecond)
    assert run.created_at.utcoffset() == timedelta(0)
    assert run.ended_at == ENDED_DT


def test_read_runs_page_data_gives_an_empty_tuple_for_an_empty_runs_table(viewer, home):
    make_run_db(home)
    runs = viewer.read_runs_page_data().runs
    assert runs == ()
    assert type(runs) is tuple
    assert runs is not viewer.UNREADABLE


@pytest.mark.parametrize("break_database", UNREADABLE_ROWS_DATABASES)
def test_read_runs_page_data_runs_is_unreadable_when(viewer, home, break_database):
    break_database(home)
    data = viewer.read_runs_page_data()
    assert isinstance(data, viewer.RunsPageData)
    assert data.runs is viewer.UNREADABLE
    assert data.runs != ()


# -------------------------------------------------------------------- render_runs_page


@pytest.mark.parametrize(
    "runs, body",
    [
        pytest.param(
            lambda viewer: [run_summary(viewer)],
            RUNS_TABLE_OPEN + run_row(7, "accepted", RUN_REQUEST) + TABLE_CLOSE,
            id="one_run",
        ),
        pytest.param(lambda viewer: [], NO_RUNS_LINE, id="no_runs"),
        pytest.param(lambda v: v.UNREADABLE, RUNS_UNAVAILABLE_LINE, id="unreadable_runs"),
    ],
)
def test_render_runs_page_is_exactly_the_planned_document_for(viewer, runs, body):
    page = render_runs(viewer, runs(viewer))
    assert page == document("Runs - tomfactory viewer", "Runs", body)


NASTY_STATE = '<i>"working"</i>'

# Keyword arguments for ``run_summary``, and the arguments of the ``run_row`` shown
# when the page is read 3m 7s after the run was created.
RUN_ROWS = [
    pytest.param({}, (7, "accepted", RUN_REQUEST), id="an_ended_run"),
    pytest.param(
        NOT_ENDED, (7, "working", RUN_REQUEST, CREATED_TEXT, "3m 7s so far"),
        id="a_running_run_says_so_far",
    ),
    pytest.param(
        {"created_at": None}, (7, "accepted", RUN_REQUEST, "unknown", "unknown"),
        id="no_created_at_gives_unknown_created_and_duration",
    ),
    pytest.param(
        {"ended_at": None}, (7, "accepted", RUN_REQUEST, CREATED_TEXT, "unknown"),
        id="an_ended_run_without_ended_at_has_unknown_duration",
    ),
    pytest.param(
        {**NOT_ENDED, "created_at": None},
        (7, "working", RUN_REQUEST, "unknown", "unknown"),
        id="an_unknown_running_duration_does_not_say_so_far",
    ),
    pytest.param(
        {**NOT_ENDED, "state": NASTY_STATE},
        (7, html.escape(NASTY_STATE), RUN_REQUEST, CREATED_TEXT, "3m 7s so far"),
        id="markup_in_the_state_is_escaped",
    ),
]  # fmt: skip


@pytest.mark.parametrize("run, row", RUN_ROWS)
def test_render_runs_page_shows_the_exact_row_of(viewer, run, row):
    assert run_row(*row) in render_runs(viewer, [run_summary(viewer, **run)])


def test_render_runs_page_keeps_rows_in_the_order_given(viewer):
    page = render_runs(viewer, [run_summary(viewer, run_id=n) for n in (3, 9, 1)])
    assert re.findall(r'<tr id="run-(\d+)">', page) == ["3", "9", "1"]


PUNCTUATED = "Fix it, now: one-two (three) four. five! six? seven; eight"

# A stored request, and the request cell shown for it: the first ten words, escaped.
REQUESTS = [
    pytest.param(TEN_WORDS, TEN_WORDS, id="ten_words_in_full"),
    pytest.param(TEN_WORDS + " eleven", TEN_WORDS + "…", id="eleven_words_cut_to_ten"),
    pytest.param(TEN_WORDS[:-4], TEN_WORDS[:-4], id="nine_words_in_full"),
    pytest.param("Fix", "Fix", id="one_word_in_full"),
    pytest.param(" \n\t  \n", "", id="whitespace_only_gives_an_empty_cell"),
    pytest.param(
        "Add a page\n\nat /runs\tlisting\r\nevery   run",
        "Add a page at /runs listing every run",
        id="newlines_and_tabs_collapse_to_spaces",
    ),
    pytest.param(TEN_WORDS + "   \n\n", TEN_WORDS, id="trailing_whitespace_no_ellipsis"),
    pytest.param(
        "<b>one</b> two three four five six seven eight nine & eleven",
        "&lt;b&gt;one&lt;/b&gt; two three four five six seven eight nine &amp;…",
        id="markup_is_cut_then_escaped",
    ),
    pytest.param(PUNCTUATED, PUNCTUATED, id="ten_words_with_punctuation_in_full"),
    pytest.param("a - b c d e f g h i j", "a - b c d e f g h i…", id="a_lone_dash_is_a_word"),
    pytest.param("café ✓ naïve 日本語 über", "café ✓ naïve 日本語 über", id="non_ascii_in_full"),
    pytest.param("α β γ δ ε ζ η θ ι κ λ", "α β γ δ ε ζ η θ ι κ…", id="non_ascii_cut_to_ten"),
]


@pytest.mark.parametrize("request_text, shown", REQUESTS)
def test_render_runs_page_shows_the_first_ten_words_of_a_request(
    viewer, request_text, shown
):
    page = render_runs(viewer, [run_summary(viewer, request=request_text)])
    assert run_row(7, "accepted", shown) in page


# How long an ended run lasted, in seconds, and the duration shown: whole seconds,
# with no unit above the largest that is not zero.
DURATIONS = [
    (0, "0s"), (1, "1s"), (42, "42s"), (59, "59s"), (60, "1m 0s"), (187, "3m 7s"),
    (187.4, "3m 7s"), (3599, "59m 59s"), (3600, "1h 0m 0s"), (7207, "2h 0m 7s"),
    (86399, "23h 59m 59s"), (86400, "1d 0h 0m 0s"), (86400 + 187, "1d 0h 3m 7s"),
    (40 * 86400 + 3661, "40d 1h 1m 1s"),
]  # fmt: skip


@pytest.mark.parametrize("seconds, text", DURATIONS)
def test_render_runs_page_formats_the_duration_of_an_ended_run_lasting(
    viewer, seconds, text
):
    page = render_runs(viewer, [run_summary(viewer, ended_at=at(seconds))])
    assert run_row(7, "accepted", RUN_REQUEST, duration=text) in page


# ------------------------------------------------------------------ read_run_page_data


def test_read_run_page_data_maps_every_field_of_a_run_and_its_steps(viewer, home):
    both = checks_json(("guard", True), ("tests", False))
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {"step_id": 11, "name": "red", "persona": "test-writer", "attempt": 1}
            | {"passed": 1, "checks": checks_json(("guard", True))},
            {"step_id": 12, "name": "test", "persona": "tester", "attempt": 2}
            | {"passed": 0, "checks": both},
        ],
    )
    data = viewer.read_run_page_data(7)
    assert data.run_id == 7
    assert data.run == viewer.RunDetail(
        viewer.RunSummary(7, "ended", "accepted", RUN_REQUEST, CREATED_DT, ENDED_DT),
        RUN_TARGET,
        RUN_BRANCH,
        RUN_REASON,
        3,
    )
    assert data.steps == (passed_step(viewer), failed_step(viewer))


def test_read_run_page_data_returns_the_run_with_the_id_asked_for(viewer, home):
    make_run_db(
        home,
        runs=[
            {"run_id": 6, "request": "the one before"},
            {"run_id": 7, "request": "the one asked for"},
            {"run_id": 8, "request": "the one after"},
        ],
    )
    data = viewer.read_run_page_data(7)
    assert data.run.summary.id == 7
    assert data.run.summary.request == "the one asked for"
    assert viewer.read_run_page_data(8).run.summary.request == "the one after"
    assert viewer.read_run_page_data(6).run.summary.request == "the one before"


def test_read_run_page_data_gives_empty_strings_for_null_branch_reason_and_persona(
    viewer, home
):
    run = {"branch": None, "reason": None}
    make_run_db(home, runs=[run], steps=[{"step_id": 12, "persona": None}])
    data = viewer.read_run_page_data(7)
    assert data.run == run_detail(viewer, branch="", reason="")
    assert data.steps == (step_summary(viewer, persona=""),)


def test_read_run_page_data_works_when_home_path_contains_a_space(
    viewer, tmp_path, monkeypatch
):
    spaced = tmp_path / "factory home"
    make_run_db(spaced, runs=[{}], steps=[{}])
    monkeypatch.setenv("TOMFACTORY_HOME", str(spaced))
    data = viewer.read_run_page_data(7)
    assert data.run == run_detail(viewer)
    assert len(data.steps) == 1


def test_read_run_page_data_orders_steps_by_id_not_by_insertion_order(viewer, home):
    make_run_db(home, runs=[{}], steps=[{"step_id": n} for n in (30, 4, 200, 12)])
    steps = viewer.read_run_page_data(7).steps
    assert [step.id for step in steps] == [4, 12, 30, 200]


def test_read_run_page_data_returns_only_the_steps_of_the_run_asked_for(viewer, home):
    make_run_db(
        home,
        runs=[{"run_id": 6}, {"run_id": 7}, {"run_id": 8}],
        steps=[
            {"run_id": 6, "name": "of six"},
            {"run_id": 7, "name": "first of seven"},
            {"run_id": 8, "name": "of eight"},
            {"run_id": 7, "name": "second of seven"},
            {"run_id": 6, "name": "also of six"},
        ],
    )
    steps = viewer.read_run_page_data(7).steps
    assert [step.name for step in steps] == ["first of seven", "second of seven"]
    assert [step.id for step in steps] == [2, 4]
    assert [step.name for step in viewer.read_run_page_data(8).steps] == ["of eight"]


def test_read_run_page_data_gives_a_step_not_yet_finished_no_ended_at_and_no_checks(
    viewer, home
):
    make_run_db(home, runs=[{}], steps=[{"step_id": 12, **UNFINISHED}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step == viewer.StepSummary(
        12, "test", "tester", 1, CREATED_DT, None, None, ()
    )
    assert step.ended_at is None
    assert step.checks == ()
    assert step.checks is not viewer.UNREADABLE


@pytest.mark.parametrize("stored, passed", [(1, True), (0, False), (None, None)])
def test_read_run_page_data_passed_is_true_false_or_none_for_a_stored(
    viewer, home, stored, passed
):
    make_run_db(home, runs=[{}], steps=[{"passed": stored}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.passed is passed


ORDERED_CHECKS = [("zeta", True), ("alpha", False), ("mid", True), ("alpha", True)]


@pytest.mark.parametrize(
    "stored, pairs",
    [
        pytest.param("[]", [], id="an_empty_json_list_gives_an_empty_tuple"),
        pytest.param(checks_json(*ORDERED_CHECKS), ORDERED_CHECKS, id="the_stored_order"),
    ],
)
def test_read_run_page_data_checks_are_read_as(viewer, home, stored, pairs):
    make_run_db(home, runs=[{}], steps=[{"checks": stored}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == tuple(viewer.Check(*pair) for pair in pairs)
    assert step.checks is not viewer.UNREADABLE


# Stored values that are not a JSON list of objects, each with a text name and a true
# or false passed.
BAD_CHECKS = [
    "not json at all", "", '[{"name": "guard", "passed": true}',
    '{"name": "guard", "passed": true}', '"guard"', "42", "null", "true", '["guard"]',
    "[[]]", "[null]", '[{"passed": true}]', '[{"name": "guard"}]', "[{}]",
    '[{"name": 5, "passed": true}]', '[{"name": null, "passed": true}]',
    '[{"name": "guard", "passed": 1}]', '[{"name": "guard", "passed": "true"}]',
    '[{"name": "guard", "passed": null}]',
    '[{"name": "guard", "passed": true}, {"name": "tests"}]', b"\x00\x01binary",
]  # fmt: skip


@pytest.mark.parametrize("stored", BAD_CHECKS)
def test_read_run_page_data_checks_are_unreadable_when_stored_as(viewer, home, stored):
    make_run_db(home, runs=[{}], steps=[{"step_id": 12, "checks": stored}])
    data = viewer.read_run_page_data(7)
    (step,) = data.steps
    assert step.checks is viewer.UNREADABLE
    assert step == step_summary(viewer, checks=viewer.UNREADABLE)
    assert data.run == run_detail(viewer)


def test_read_run_page_data_keeps_the_other_steps_when_one_has_unreadable_checks(
    viewer, home
):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {"step_id": 1, "checks": checks_json(("guard", True))},
            {"step_id": 2, "checks": "not json", "passed": 0},
            {"step_id": 3, "checks": checks_json(("tests", False)), "passed": 0},
            {"step_id": 4, **UNFINISHED},
        ],
    )
    data = viewer.read_run_page_data(7)
    assert data.steps is not viewer.UNREADABLE
    assert [step.id for step in data.steps] == [1, 2, 3, 4]
    assert [step.checks for step in data.steps] == [
        (viewer.Check("guard", True),),
        viewer.UNREADABLE,
        (viewer.Check("tests", False),),
        (),
    ]
    assert data.steps[1].passed is False


def test_read_run_page_data_runs_no_statement_on_steps_for_an_unknown_id(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{"run_id": 7}, {"run_id": 99}])
    _, proxies = wrap_connections(viewer, monkeypatch)

    data = viewer.read_run_page_data(99)

    assert data.run is None
    assert data.steps == ()
    assert len(proxies) == 1
    assert any("runs" in sql.lower() for sql in proxies[0].statements)
    assert not any("steps" in sql.lower() for sql in proxies[0].statements)
    assert proxies[0].close_calls == 1


def test_read_run_page_data_steps_are_empty_not_unreadable_for_an_unknown_id_without_a_steps_table(
    viewer, home
):
    make_run_db(home, runs=[{}], steps_table=False)
    data = viewer.read_run_page_data(99)
    assert data.run is None
    assert data.steps == ()
    assert data.steps is not viewer.UNREADABLE


@pytest.mark.parametrize("break_database", UNREADABLE_ROWS_DATABASES)
def test_read_run_page_data_run_and_steps_are_unreadable_when(
    viewer, home, break_database
):
    break_database(home)
    data = viewer.read_run_page_data(7)
    assert isinstance(data, viewer.RunPageData)
    assert data.run is viewer.UNREADABLE
    assert data.steps is viewer.UNREADABLE
    assert data.run_id == 7


def test_read_run_page_data_keeps_the_run_and_marks_steps_unreadable_without_a_steps_table(
    viewer, home
):
    make_run_db(home, runs=[{}], steps_table=False)
    data = viewer.read_run_page_data(7)
    assert data.run == run_detail(viewer)
    assert data.steps is viewer.UNREADABLE
    assert data.steps != ()


def test_read_run_page_data_run_and_steps_come_from_the_same_moment(
    viewer, home, monkeypatch
):
    path = home / "factory.db"
    insert = (
        "INSERT INTO steps (run_id, name, kind, attempt, asked, started_at) "
        f"VALUES (7, 'build', 'agent', 1, 'do the work', '{CREATED}')"
    )

    def seed(path):
        insert_detail_run(path)
        insert_step(path, name="plan")

    def reads_the_run(sql):
        return "runs" in sql and "steps" not in sql

    with write_mid_read(
        viewer, monkeypatch, path, RUNS_DDL + STEPS_DDL, reads_the_run, insert, seed
    ) as (inserted, proxies):
        data = viewer.read_run_page_data(7)

        assert inserted == ["attempted", "committed"], (
            "the second step must be written between the run read and the steps read"
        )
        assert len(proxies) == 1
        assert data.run == run_detail(viewer)
        assert [step.name for step in data.steps] == ["plan"]

        # The second step really was committed: the next read shows it.
        steps = viewer.read_run_page_data(7).steps
        assert [step.name for step in steps] == ["plan", "build"]


# --------------------------------------------------------------------- render_run_page

# How the page is rendered, its heading (which also starts its title), and its body.
RUN_DOCUMENTS = [
    pytest.param(
        lambda v: render_run(v, run_detail(v), [passed_step(v), failed_step(v)]), "Run 7",
        run_facts() + STEPS_TABLE_OPEN + PASSED_STEP_ROW + FAILED_STEP_ROW + TABLE_CLOSE,
        id="a_run_with_steps",
    ),
    pytest.param(
        lambda v: render_run(v, run_detail(v), []), "Run 7", run_facts() + NO_STEPS_LINE,
        id="a_run_with_no_steps",
    ),
    pytest.param(
        lambda v: render_run(v, run_detail(v), v.UNREADABLE), "Run 7",
        run_facts() + STEPS_UNAVAILABLE_LINE,
        id="unreadable_steps",
    ),
    pytest.param(
        lambda v: render_run(v, v.UNREADABLE, v.UNREADABLE), "Run 7",
        RUN_UNAVAILABLE_LINE,
        id="an_unreadable_run",
    ),
    pytest.param(
        lambda v: render_run(v, None, (), run_id=99), "Run not found",
        run_not_found_line(99),
        id="a_run_not_found",
    ),
]  # fmt: skip


@pytest.mark.parametrize("render, heading, body", RUN_DOCUMENTS)
def test_render_run_page_is_exactly_the_planned_document_for(
    viewer, render, heading, body
):
    assert render(viewer) == document(f"{heading} - tomfactory viewer", heading, body)


@pytest.mark.parametrize("state, outcome, ended, status, label", RUN_STATUSES)
def test_render_run_page_labels_the_status_and_gives_the_reason_by_the_outcome(
    viewer, state, outcome, ended, status, label
):
    page = render_run(viewer, run_detail(viewer, state=state, outcome=outcome))
    reason = RUN_REASON if ended else None
    assert page == document(
        "Run 7 - tomfactory viewer",
        "Run 7",
        run_facts(label=label, status=status, reason=reason) + NO_STEPS_LINE,
    )


LONG_REQUEST = TEN_WORDS + " eleven twelve thirteen"


@pytest.mark.parametrize(
    "run, shown",
    [
        pytest.param({"reason": ""}, {"reason": None}, id="no_line_for_an_empty_reason"),
        pytest.param({"branch": ""}, {"branch": "none"}, id="none_for_an_empty_branch"),
        pytest.param(
            {"request": LONG_REQUEST}, {"request": LONG_REQUEST},
            id="a_long_request_in_full_without_an_ellipsis",
        ),
    ],
)  # fmt: skip
def test_render_run_page_shows_the_facts_of_a_run_with(viewer, run, shown):
    assert run_facts(**shown) in render_run(viewer, run_detail(viewer, **run))


# A step, and its exact row when the page is read 3m 7s after the step started.
STEP_ROWS = [
    pytest.param(passed_step, PASSED_STEP_ROW, id="a_passed_step"),
    pytest.param(failed_step, FAILED_STEP_ROW, id="a_failed_step"),
    pytest.param(
        lambda viewer: step_summary(viewer, **UNFINISHED),
        step_row(12, duration="3m 7s so far", result="not finished"),
        id="a_step_not_yet_finished_says_so_far",
    ),
    pytest.param(
        lambda viewer: step_summary(viewer, passed=False, ended_at=None),
        step_row(12, duration="unknown", result="failed"),
        id="a_finished_step_without_ended_at_has_unknown_duration",
    ),
    pytest.param(
        lambda viewer: step_summary(viewer, **UNFINISHED, started_at=None),
        step_row(12, duration="unknown", result="not finished"),
        id="an_unknown_unfinished_duration_does_not_say_so_far",
    ),
    pytest.param(
        lambda viewer: step_summary(viewer, checks=viewer.UNREADABLE),
        step_row(12, checks="unreadable"),
        id="unreadable_checks",
    ),
]


@pytest.mark.parametrize("step, row", STEP_ROWS)
def test_render_run_page_shows_the_exact_row_of(viewer, step, row):
    assert row in render_run(viewer, run_detail(viewer), [step(viewer)])


def test_render_run_page_shows_other_steps_checks_beside_unreadable_ones(viewer):
    steps = [
        step_summary(viewer, step_id=1, checks=(viewer.Check("guard", True),)),
        step_summary(viewer, step_id=2, checks=viewer.UNREADABLE),
        step_summary(viewer, step_id=3, checks=()),
    ]
    page = render_run(viewer, run_detail(viewer), steps)
    assert step_row(1, checks='<span class="check">guard</span>') in page
    assert step_row(2, checks="unreadable") in page
    assert step_row(3, checks="") in page
    assert "steps-unavailable" not in page


def test_render_run_page_keeps_step_rows_in_the_order_given(viewer):
    steps = [step_summary(viewer, step_id=n) for n in (30, 4, 12)]
    page = render_run(viewer, run_detail(viewer), steps)
    assert re.findall(r'<tr id="step-(\d+)">', page) == ["30", "4", "12"]


def test_render_run_page_escapes_every_stored_text_at_once(viewer):
    nasty = "<u>&\"'"
    run = run_detail(
        viewer, request=nasty, target=nasty, branch=nasty, outcome=nasty, reason=nasty
    )
    checks = (viewer.Check(nasty, True), viewer.Check(nasty, False))
    step = step_summary(viewer, name=nasty, persona=nasty, passed=False, checks=checks)
    page = render_run(viewer, run, [step])
    assert "<u>" not in page
    assert nasty not in page
    assert page.count(html.escape(nasty)) == 9


# ----------------------------------------------------------------- read_step_page_data


def test_read_step_page_data_maps_every_field_of_a_step_and_its_events_in_id_order(
    viewer, home
):
    failed = {"name": "tests", "passed": False, "note": "1 failed", "timed_out": False}
    produced = {"summary": "done", "files": ["a.py"]}
    step = {"step_id": 12, "attempt": 2, "passed": 0, "checks": json.dumps([failed])}
    step |= {"asked": "write the tests", "produced": json.dumps(produced)}
    events = [
        {"event_id": 30, "step_id": 12, "kind": "step_end", "summary": "finished"},
        {"event_id": 4, "step_id": 12, "at": ENDED, "kind": "step_start"},
        {"event_id": 5, "step_id": 13, "summary": "of another step"},
        {"event_id": 6, "step_id": None, "summary": "of the run alone"},
    ]
    make_run_db(home, runs=[{}], steps=[step, {"step_id": 13}], events=events)
    data = viewer.read_step_page_data(7, 12)
    checks = (viewer.Check("tests", False, "1 failed"),)
    detail = step_detail(
        viewer, "write the tests", produced, attempt=2, passed=False, checks=checks
    )
    began = viewer.Event(4, ENDED_DT, "step_start", "ran")
    ended = viewer.Event(30, CREATED_DT, "step_end", "finished")
    assert data == viewer.StepPageData(7, 12, detail, (began, ended), data.read_at)


# Keyword arguments for ``make_run_db``: step 1 is of run 7 and step 2 of run 8, each
# with an event. Then the steps that are not there, as (run id, step id).
BOTH = [{}, {"run_id": 8}]
TWO_RUNS = {"runs": BOTH, "steps": BOTH, "events": [{}, {"run_id": 8, "step_id": 2}]}
MISSING_STEPS = [(7, 99), (7, 2), (99, 1)]
MISSING_STEP_IDS = ["an_unknown_step", "a_step_of_another_run", "an_unknown_run"]


@pytest.mark.parametrize("run_id, step_id", MISSING_STEPS, ids=MISSING_STEP_IDS)
def test_read_step_page_data_gives_no_step_and_runs_no_statement_on_events_for(
    viewer, home, monkeypatch, run_id, step_id
):
    make_run_db(home, **TWO_RUNS)
    _, proxies = wrap_connections(viewer, monkeypatch)

    data = viewer.read_step_page_data(run_id, step_id)

    assert data[:4] == (run_id, step_id, None, ())
    assert any("steps" in sql.lower() for sql in proxies[0].statements)
    assert not any("events" in sql.lower() for sql in proxies[0].statements)


# A stored ``steps.produced``, and what is read from it: ``None`` stands for UNREADABLE.
PRODUCED = [
    pytest.param(None, {}, id="null_gives_an_empty_object"),
    pytest.param('{"b": 1, "a": "x"}', {"b": 1, "a": "x"}, id="an_object_in_key_order"),
    pytest.param("not json", None, id="not_json"),
    pytest.param('["a"]', None, id="a_list"),
    pytest.param("42", None, id="a_number"),
    pytest.param(b"\x00\x01binary", None, id="a_blob"),
]


@pytest.mark.parametrize("stored, expected", PRODUCED)
def test_read_step_page_data_produced_is_read_from(viewer, home, stored, expected):
    make_run_db(home, runs=[{}], steps=[{"produced": stored}])
    step = viewer.read_step_page_data(7, 1).step
    expected = viewer.UNREADABLE if expected is None else expected
    assert step.produced == expected
    assert expected is viewer.UNREADABLE or list(step.produced) == list(expected)
    assert step.summary == step_summary(viewer, step_id=1)


@pytest.mark.parametrize(
    "check, note",
    [({"note": "1 failed\n<b>"}, "1 failed\n<b>"), ({}, ""), ({"note": None}, "")]
    + [({"note": 5}, "")],
    ids=["text_is_kept", "missing_is_empty", "null_is_empty", "a_number_is_empty"],
)
def test_read_step_page_data_note_of_a_check_when(viewer, home, check, note):
    stored = json.dumps([{"name": "guard", "passed": False, **check}])
    make_run_db(home, runs=[{}], steps=[{"checks": stored}])
    checks = viewer.read_step_page_data(7, 1).step.summary.checks
    assert checks == (viewer.Check("guard", False, note),)


# -------------------------------------------------------------------- render_step_page

# How the page is rendered, its heading (which also starts its title), and its body.
STEP_DOCUMENTS = [
    pytest.param(
        lambda v: render_step(v, *full_step(v)), "Step 12 of run 7",
        step_body(attempt=2, result="failed", asked="write it", **FULL_BLOCKS),
        id="a_full_step",
    ),
    pytest.param(
        lambda v: render_step(v, step_detail(v, persona="", **UNFINISHED)),
        "Step 12 of run 7",
        step_body(persona="none", result="not finished", duration="3m 7s so far"),
        id="an_unfinished_step_with_no_persona_and_every_block_empty",
    ),
    pytest.param(
        lambda v: render_step(
            v, step_detail(v, produced=v.UNREADABLE, checks=v.UNREADABLE), v.UNREADABLE
        ),
        "Step 12 of run 7",
        step_body(
            produced=UNAVAILABLE_LINE.format("produced", "Produced", "what the step produced"),
            checks=UNAVAILABLE_LINE.format("checks", "Checks", "the checks of this step"),
            events=UNAVAILABLE_LINE.format("events", "Events", "the events table"),
        ),
        id="every_block_unreadable",
    ),
    pytest.param(
        lambda v: render_step(v, v.UNREADABLE, v.UNREADABLE), "Step 12 of run 7",
        UNAVAILABLE_LINE.format("step", "Step", "the steps table"),
        id="an_unreadable_step",
    ),
    pytest.param(
        lambda v: render_step(v, None, step_id=99), "Step not found",
        STEP_NOT_FOUND_LINE.format(99, 7), id="a_step_not_found",
    ),
]  # fmt: skip


@pytest.mark.parametrize("render, heading, body", STEP_DOCUMENTS)
def test_render_step_page_is_exactly_the_planned_document_for(
    viewer, render, heading, body
):
    assert render(viewer) == document(f"{heading} - tomfactory viewer", heading, body)


def test_render_step_page_escapes_every_stored_text_at_once(viewer):
    nasty = "<u>&\"'"
    checks = (viewer.Check(nasty, False, nasty),)
    step = step_detail(
        viewer, nasty, {nasty: nasty}, name=nasty, persona=nasty, checks=checks
    )
    page = render_step(viewer, step, (viewer.Event(1, CREATED_DT, nasty, nasty),))
    assert nasty not in page
    assert page.count(html.escape(nasty)) == 9


# --------------------------------------------------------------------- the HTTP server

# Each page: its path, and the page the two lower layers give for it.
PAGES = [
    pytest.param("/", lambda v: v.render_page(v.read_page_data()), id="home"),
    pytest.param(
        "/runs", lambda v: v.render_runs_page(v.read_runs_page_data()), id="runs"
    ),
    pytest.param(
        "/runs/7", lambda v: v.render_run_page(v.read_run_page_data(7)), id="run"
    ),
    pytest.param(
        "/runs/7/steps/1", lambda v: v.render_step_page(v.read_step_page_data(7, 1)),
        id="step",
    ),
]  # fmt: skip


@pytest.mark.parametrize("query", ["", "?x=1"], ids=["no_query", "query_string"])
@pytest.mark.parametrize("path, page", PAGES)
def test_get_answers_200_with_the_page_rendered_from_what_was_read(
    viewer, home, port, path, page, query
):
    # Every run and step here has ended, so no page depends on when it was read.
    run = {"request": "café ✓", "reason": "terminé ✓"}
    make_run_db(home, runs=[run], steps=[{"asked": "écris ✓"}], events=[{}])
    status, headers, body = fetch(port, path + query)
    assert status == 200
    assert headers["Content-Type"].replace(" ", "").lower() == "text/html;charset=utf-8"
    assert int(headers["Content-Length"]) == len(body)
    assert len(body) > len(body.decode("utf-8"))
    assert body.decode("utf-8") == page(viewer)


@pytest.mark.parametrize("path, page", PAGES)
def test_get_answers_200_with_the_unavailable_page_when_there_is_no_database(
    viewer, home, port, path, page
):
    status, _, body = fetch(port, path)
    assert status == 200
    assert "unavailable" in body.decode("utf-8")
    assert body.decode("utf-8") == page(viewer)


def test_get_run_page_for_an_unknown_id_returns_404_with_the_run_not_found_page(
    viewer, home, port
):
    make_run_db(home, runs=[{}], steps=[{}])
    status, headers, body = fetch(port, "/runs/99")
    text = body.decode("utf-8")
    assert status == 404
    assert headers["Content-Type"].replace(" ", "").lower() == "text/html;charset=utf-8"
    assert int(headers["Content-Length"]) == len(body)
    assert 'id="run-not-found"' in text
    assert text == document(
        "Run not found - tomfactory viewer", "Run not found", run_not_found_line(99)
    )
    assert text == viewer.render_run_page(viewer.RunPageData(99, None, (), ENDED_DT))


@pytest.mark.parametrize("run_id, step_id", MISSING_STEPS, ids=MISSING_STEP_IDS)
def test_get_step_page_answers_404_with_the_step_not_found_page_for(
    home, port, run_id, step_id
):
    make_run_db(home, **TWO_RUNS)
    status, _, body = fetch(port, f"/runs/{run_id}/steps/{step_id}")
    line = STEP_NOT_FOUND_LINE.format(step_id, run_id)
    page = document("Step not found - tomfactory viewer", "Step not found", line)
    assert (status, body.decode("utf-8")) == (404, page)


# Paths that are not served: not a page, or not the page of a run with a plain id of
# at most eighteen digits.
NOT_RUN_PAGE_PATHS = (
    "/nope /index.html /runs.html /runs/ /runs/abc /runs/0 /runs/07 /runs/007 /runs/7/ "
    "/runs/1234567890123456789 /runs/-7 /runs/+7 /runs/7.0 /runs/7abc /runs/abc7 "
    "/runs/0x7 /runs/7/steps /runs//7 /runs/7/?x=1 /Runs/7 /run/7 /7 "
    "/runs/7/steps/ /runs/7/steps/0 /runs/7/steps/abc /runs/7/steps/1/"
).split()


@pytest.mark.parametrize("path", NOT_RUN_PAGE_PATHS)
def test_get_run_page_is_not_served_and_the_database_is_not_opened_at(
    viewer, home, port, monkeypatch, path
):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{}])
    assert fetch(port, "/runs/7")[0] == 200
    calls, _ = wrap_connections(viewer, monkeypatch)

    status, _, body = fetch(port, path)

    assert status == 404
    assert calls == []
    assert b"run-not-found" not in body
    assert b'id="run"' not in body
    assert b"<h1>Run 7</h1>" not in body


def test_get_run_page_accepts_an_id_of_eighteen_digits_but_not_of_nineteen(
    viewer, home, port, monkeypatch
):
    make_run_db(home, runs=[{}])
    calls, _ = wrap_connections(viewer, monkeypatch)

    status, _, body = fetch(port, "/runs/100000000000000000")
    assert status == 404
    assert run_not_found_line(100000000000000000) in body.decode("utf-8")
    assert_one_read_only_connect(calls)

    status, _, body = fetch(port, "/runs/1000000000000000000")
    assert status == 404
    assert b"run-not-found" not in body
    assert len(calls) == 1


def test_the_runs_link_on_the_home_page_leads_on_to_the_page_of_a_run(home, port):
    make_run_db(home, runs=[{"request": "reached from the home page"}], steps=[{}])
    _, _, home_body = fetch(port)
    runs_link = re.search(
        r'<a id="runs-link" href="([^"]+)">', home_body.decode("utf-8")
    )
    assert runs_link is not None
    _, _, runs_body = fetch(port, runs_link.group(1))
    run_link = re.search(
        r'<td class="run-id"><a href="([^"]+)">7</a></td>', runs_body.decode("utf-8")
    )
    assert run_link is not None
    status, _, run_body = fetch(port, run_link.group(1))
    assert status == 200
    assert (
        '<dd id="run-request">reached from the home page</dd>'
        in run_body.decode("utf-8")
    )
    step_link = re.search(
        r'<td class="step-name"><a href="([^"]+)">test</a></td>', run_body.decode("utf-8")
    )
    assert step_link is not None
    status, _, step_body_bytes = fetch(port, step_link.group(1))
    assert status == 200
    assert "<h1>Step 1 of run 7</h1>" in step_body_bytes.decode("utf-8")


def test_post_to_root_is_not_implemented(home, port):
    make_db(home, rows=3)
    status, _, _ = fetch(port, "/", method="POST")
    assert status == 501


def test_server_still_serves_root_after_a_404(home, port):
    make_db(home, rows=3)
    assert fetch(port, "/nope")[0] == 404
    assert fetch(port, "/")[0] == 200


def test_make_server_binds_to_localhost(viewer, home):
    server = viewer.make_server(port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] != 0
    finally:
        server.server_close()


# ----------------------------------------------------- main, and the standard library


@pytest.mark.parametrize(
    "argv, asked",
    [([], 8000), (["--port", "9000"], 9000)],
    ids=["port_8000_by_default", "the_port_given_by_the_port_option"],
)
def test_main_serves_until_interrupted_then_closes_the_server_on(
    viewer, home, monkeypatch, capsys, argv, asked
):
    fake, requested = run_main_with_fake_server(viewer, monkeypatch, argv)
    assert requested == asked
    assert fake.served
    assert fake.closed
    assert f"Serving on http://127.0.0.1:{asked}/" in capsys.readouterr().out


def test_main_rejects_a_port_that_is_not_a_number(viewer, home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        viewer, "make_server", lambda *a, **k: calls.append((a, k)) or FakeServer()
    )
    with pytest.raises(SystemExit) as excinfo:
        viewer.main(["--port", "not-a-port"])
    assert excinfo.value.code == 2
    assert calls == []


def test_viewer_imports_only_the_standard_library(viewer):
    tree = ast.parse(Path(viewer.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not expected in viewer.py"
            imported.add(node.module.split(".")[0])

    assert "sqlite3" in imported
    assert imported <= set(sys.stdlib_module_names), (
        f"non-stdlib imports: {sorted(imported - set(sys.stdlib_module_names))}"
    )
