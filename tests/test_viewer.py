"""Tests for the hello-world observability page (``viewer.py`` at the repository root)."""

import ast
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


# --------------------------------------------------------------------------- helpers

OUTCOME_LINE = '<p>Latest run outcome: <strong id="latest-run-outcome">{}</strong></p>\n'
REASON_LINE = '<p>Latest run reason: <span id="latest-run-reason">{}</span></p>\n'
NOT_ENDED_LINE = '<p id="latest-run-not-ended">Latest run has not ended yet</p>\n'
LATEST_UNAVAILABLE_LINE = (
    '<p id="latest-run-unavailable">Latest run unavailable: '
    "could not read outcome and reason from the runs table</p>\n"
)
COUNT_LINE = '<p>Runs recorded: <strong id="run-count">{}</strong></p>\n'


def count_unavailable_line(path):
    """The line the page shows when the count could not be read from ``path``."""
    return (
        '<p id="run-count-unavailable">Run count unavailable: '
        f"no readable runs table in {html.escape(str(path))}</p>\n"
    )


def make_db(home, rows=0, with_runs_table=True, columns=""):
    """Create ``home/factory.db``; optionally with a ``runs`` table holding ``rows`` rows.

    The ``runs`` table made here has an ``id`` plus the raw column SQL in ``columns``.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "factory.db"
    conn = sqlite3.connect(path)
    try:
        if with_runs_table:
            conn.execute(f"CREATE TABLE runs (id INTEGER PRIMARY KEY{columns})")
            conn.executemany("INSERT INTO runs DEFAULT VALUES", [()] * rows)
        else:
            conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    return path


def execute_sql(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


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


def record_connects(viewer, monkeypatch):
    """Replace ``viewer.sqlite3.connect`` with a recorder; return the list of calls.

    ``viewer.sqlite3`` is the ``sqlite3`` module itself, so build the database before
    calling this: every later ``sqlite3.connect`` call is recorded.
    """
    calls = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(viewer.sqlite3, "connect", recording_connect)
    return calls


def assert_one_read_only_connect(calls):
    assert len(calls) == 1
    args, kwargs = calls[0]
    database = args[0] if args else kwargs["database"]
    assert "mode=ro" in str(database)
    assert kwargs.get("uri") is True


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
    """Make ``viewer.sqlite3.connect`` return proxies; return the list of proxies made."""
    proxies = []
    real_connect = sqlite3.connect

    def wrapping_connect(*args, **kwargs):
        proxy = ConnectionProxy(real_connect(*args, **kwargs), after_execute)
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(viewer.sqlite3, "connect", wrapping_connect)
    return proxies


# Ways for the whole database to be unreadable. Each takes the home directory.


def _leave_database_missing(home):
    pass


def _remove_home_directory(home):
    home.rmdir()


def _write_empty_file(home):
    (home / "factory.db").write_bytes(b"")


def _write_non_sqlite_file(home):
    (home / "factory.db").write_bytes(b"this is definitely not a sqlite database\n" * 50)


def _make_factory_db_a_directory(home):
    (home / "factory.db").mkdir()


def _make_database_without_runs_table(home):
    make_db(home, with_runs_table=False)


UNREADABLE_DATABASES = [
    pytest.param(_leave_database_missing, id="database_file_is_missing"),
    pytest.param(_remove_home_directory, id="home_directory_is_missing"),
    pytest.param(_write_empty_file, id="database_file_is_empty"),
    pytest.param(_write_non_sqlite_file, id="file_is_not_a_sqlite_database"),
    pytest.param(_make_factory_db_a_directory, id="factory_db_is_a_directory"),
    pytest.param(_make_database_without_runs_table, id="runs_table_is_missing"),
]

# Each reader of the data layer, as a function of the module under test.
READERS = [
    pytest.param(lambda viewer: viewer.read_page_data(), id="read_page_data"),
    pytest.param(lambda viewer: viewer.read_runs_page_data(), id="read_runs_page_data"),
    pytest.param(lambda viewer: viewer.read_run_page_data(7), id="read_run_page_data"),
]

# Database states every reader must cope with: how to build each, and how many
# connections to it can be opened at all.
DATABASE_STATES = [
    pytest.param(lambda home: make_run_db(home, runs=[{}], steps=[{}]), 1, id="readable"),
    pytest.param(lambda home: make_run_db(home), 1, id="empty_tables"),
    pytest.param(_make_database_without_runs_table, 1, id="runs_table_is_missing"),
    pytest.param(_write_non_sqlite_file, 1, id="file_is_not_a_sqlite_database"),
    pytest.param(_leave_database_missing, 0, id="database_file_is_missing"),
    pytest.param(_remove_home_directory, 0, id="home_directory_is_missing"),
]


def facts(data):
    """What a hand-off says was read: every field but ``run_id`` and ``read_at``."""
    return [v for k, v in data._asdict().items() if k not in ("run_id", "read_at")]


# -------------------------------------------------------------------------- fixtures


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


# --------------------------------------------------------------------------- db_path


def test_db_path_is_factory_db_inside_tomfactory_home(viewer, home):
    assert viewer.db_path() == home / "factory.db"


def test_db_path_defaults_to_dot_tomfactory_in_home_when_variable_unset(
    viewer, tmp_path, monkeypatch
):
    monkeypatch.delenv("TOMFACTORY_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert viewer.db_path() == tmp_path / ".tomfactory" / "factory.db"


def test_db_path_defaults_to_dot_tomfactory_in_home_when_variable_empty(
    viewer, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOMFACTORY_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert viewer.db_path() == tmp_path / ".tomfactory" / "factory.db"


def test_db_path_reads_the_environment_on_every_call(viewer, tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("TOMFACTORY_HOME", str(first))
    assert viewer.db_path() == first / "factory.db"
    monkeypatch.setenv("TOMFACTORY_HOME", str(second))
    assert viewer.db_path() == second / "factory.db"


# ---------------------------------------------------- UNREADABLE and the hand-off types


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
        ("Check", "name passed"),
        ("StepSummary", "id name persona attempt started_at ended_at passed checks"),
        ("RunDetail", "summary target branch reason attempt_limit"),
        ("RunPageData", "run_id run steps read_at"),
    ],
)
def test_hand_off_type_has_its_fields_in_order(viewer, name, fields):
    assert getattr(viewer, name)._fields == tuple(fields.split())


# --------------------------------------------------- read_page_data: readable states


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


# ------------------------------------------------- read_page_data: empty runs table


def test_read_page_data_returns_zero_and_none_for_an_empty_runs_table(viewer, home):
    make_run_db(home)
    assert viewer.read_page_data() == viewer.PageData(0, None)
    assert type(viewer.read_page_data().count) is int


# ----------------------------------------- read_page_data: outcome columns unreadable


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


# ------------------------------------------------ every reader: database unreadable


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


# ------------------------------------------- every reader: one read-only connection


@pytest.mark.parametrize("read", READERS)
@pytest.mark.parametrize("build, opened", DATABASE_STATES)
def test_every_reader_opens_one_read_only_connection_and_closes_it_once(
    viewer, home, monkeypatch, read, build, opened
):
    build(home)
    proxies = wrap_connections(viewer, monkeypatch)
    calls = record_connects(viewer, monkeypatch)

    read(viewer)

    assert_one_read_only_connect(calls)
    assert [proxy.close_calls for proxy in proxies] == [1] * opened


# ------------------------------------------------------ read_page_data: one moment


def test_read_page_data_count_and_latest_run_come_from_the_same_moment(
    viewer, home, monkeypatch
):
    path = home / "factory.db"
    # Kept open so the database stays in WAL mode with its -wal and -shm files.
    holder = sqlite3.connect(path)
    try:
        mode = holder.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode.lower() == "wal"
        holder.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, outcome TEXT, reason TEXT)"
        )
        holder.execute(
            "INSERT INTO runs (outcome, reason) VALUES ('passed', 'first run')"
        )
        holder.commit()

        real_connect = sqlite3.connect
        inserted = []

        def insert_second_run_after_the_count(sql):
            if "COUNT" not in sql.upper() or inserted:
                return
            inserted.append("attempted")
            writer = real_connect(path, timeout=2)
            try:
                writer.execute(
                    "INSERT INTO runs (outcome, reason) VALUES ('failed', 'second run')"
                )
                writer.commit()
                inserted.append("committed")
            except sqlite3.Error as error:  # reported by the assertion below
                inserted.append(error)
            finally:
                writer.close()

        proxies = wrap_connections(
            viewer, monkeypatch, insert_second_run_after_the_count
        )

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
    finally:
        holder.close()


# -------------------------------------------- every reader: never writes or creates


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


# ----------------------------------------------------- every reader: between calls


@pytest.mark.parametrize("read", READERS)
def test_every_reader_sees_rows_inserted_since_its_last_call(viewer, home, read):
    path = make_run_db(home, runs=[{}])
    before = facts(read(viewer))
    insert_detail_run(path, run_id=8)
    insert_step(path)
    assert facts(read(viewer)) != before


# ------------------------------------------------------- render_page: the count line


def test_render_page_escapes_html_in_the_database_path(viewer, tmp_path, monkeypatch):
    nasty = tmp_path / "a&b<i>c"
    monkeypatch.setenv("TOMFACTORY_HOME", str(nasty))
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, None))
    assert html.escape(str(nasty / "factory.db")) in page
    assert "<i>" not in page
    assert "a&b" not in page


def test_render_page_with_zero_runs_and_no_latest_run_says_only_the_count(viewer, home):
    page = viewer.render_page(viewer.PageData(0, None))
    assert page.endswith(COUNT_LINE.format(0) + "</body>\n</html>\n")
    assert "unavailable" not in page


# ---------------------------------------- render_page: latest run could not be read


def test_render_page_puts_latest_run_unavailable_right_after_the_count_line(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))
    assert page.endswith(
        COUNT_LINE.format(3) + LATEST_UNAVAILABLE_LINE + "</body>\n</html>\n"
    )


def test_render_page_puts_count_unavailable_before_latest_run_unavailable(viewer, home):
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE))
    assert page.endswith(
        count_unavailable_line(home / "factory.db")
        + LATEST_UNAVAILABLE_LINE
        + "</body>\n</html>\n"
    )


# ------------------------------------------------------ render_page: a latest run


def test_render_page_puts_latest_run_lines_right_after_the_count_line(viewer, home):
    page = viewer.render_page(
        viewer.PageData(3, viewer.LatestRun("failed", "tests red"))
    )
    expected = (
        COUNT_LINE.format(3)
        + OUTCOME_LINE.format("failed")
        + REASON_LINE.format("tests red")
        + "</body>\n"
        "</html>\n"
    )
    assert page.endswith(expected)


def test_render_page_omits_reason_line_when_ended_run_has_empty_reason(viewer, home):
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("passed", "")))
    assert OUTCOME_LINE.format("passed") in page
    assert "latest-run-reason" not in page
    assert "Latest run reason" not in page
    assert "latest-run-not-ended" not in page


def test_render_page_shows_not_ended_and_hides_reason_when_outcome_is_empty(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("", "still going")))
    assert NOT_ENDED_LINE in page
    assert "latest-run-outcome" not in page
    assert "latest-run-reason" not in page
    assert "still going" not in page


def test_render_page_escapes_quotes_in_outcome_and_reason(viewer, home):
    page = viewer.render_page(
        viewer.PageData(3, viewer.LatestRun('say "hi"', "it's \"bad\""))
    )
    assert OUTCOME_LINE.format(html.escape('say "hi"')) in page
    assert REASON_LINE.format(html.escape("it's \"bad\"")) in page
    assert 'say "hi"' not in page


# ------------------------------------------- render_page does not read the database


def test_render_page_does_not_open_the_database_when_told_nothing_was_readable(
    viewer, home, monkeypatch
):
    def forbidden_connect(*args, **kwargs):
        raise AssertionError("render_page must not open the database")

    monkeypatch.setattr(viewer.sqlite3, "connect", forbidden_connect)

    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE))
    assert 'id="run-count-unavailable"' in page
    assert LATEST_UNAVAILABLE_LINE in page
    assert 'id="run-count"' not in page


# ----------------------------------------------------------------------- HTTP server


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


# ------------------------------------------------------------------------------ main


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
    """Run ``main(argv)`` against a fake server that is interrupted at once."""
    fake = FakeServer()
    calls = []

    def fake_make_server(*args, **kwargs):
        calls.append((args, kwargs))
        return fake

    monkeypatch.setattr(viewer, "make_server", fake_make_server)
    try:
        viewer.main(argv)
    except SystemExit as exit_:
        assert exit_.code in (None, 0)
    return fake, calls


def requested_port(calls):
    assert len(calls) == 1
    args, kwargs = calls[0]
    return kwargs["port"] if "port" in kwargs else args[1]


def test_main_serves_on_port_8000_by_default(viewer, home, monkeypatch, capsys):
    fake, calls = run_main_with_fake_server(viewer, monkeypatch, [])
    assert requested_port(calls) == 8000
    assert fake.served
    assert "Serving on http://127.0.0.1:8000/" in capsys.readouterr().out


def test_main_serves_on_the_port_given_by_the_port_option(
    viewer, home, monkeypatch, capsys
):
    fake, calls = run_main_with_fake_server(viewer, monkeypatch, ["--port", "9000"])
    assert requested_port(calls) == 9000
    assert fake.served
    assert "Serving on http://127.0.0.1:9000/" in capsys.readouterr().out


def test_main_closes_the_server_on_keyboard_interrupt(viewer, home, monkeypatch):
    fake, _ = run_main_with_fake_server(viewer, monkeypatch, [])
    assert fake.closed


def test_main_rejects_a_port_that_is_not_a_number(viewer, home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        viewer, "make_server", lambda *a, **k: calls.append((a, k)) or FakeServer()
    )
    with pytest.raises(SystemExit) as excinfo:
        viewer.main(["--port", "not-a-port"])
    assert excinfo.value.code == 2
    assert calls == []


# -------------------------------------------------------------------- standard library


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


# ====================================================================================
# The /runs page: every run, newest first
# ====================================================================================

# ------------------------------------------------------------ helpers for /runs tests

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

CREATED = "2026-11-18T21:04:05Z"
ENDED = "2026-11-18T21:07:12Z"  # 3m 7s after CREATED
CREATED_DT = datetime(2026, 11, 18, 21, 4, 5, tzinfo=timezone.utc)
ENDED_DT = CREATED_DT + timedelta(seconds=187)
CREATED_TEXT = "2026-11-18 21:04:05 UTC"

# Keyword arguments for ``insert_detail_run`` that make the run one that has not ended.
RUNNING = {"state": "working", "outcome": None, "ended_at": None}

TEN_WORDS = "one two three four five six seven eight nine ten"

RUNS_LINK_LINE = '<p><a id="runs-link" href="/runs">All runs</a></p>\n'
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
RUNS_TABLE_CLOSE = "</tbody>\n</table>\n"
DOCUMENT_TAIL = "</body>\n</html>\n"


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


def request_cell(text):
    return f'<td class="run-request">{text}</td>'


def duration_cell(text):
    return f'<td class="run-duration">{text}</td>'


def ended_summary(
    viewer,
    run_id=7,
    outcome="accepted",
    request="Fix the bug",
    created_at=CREATED_DT,
    ended_at=ENDED_DT,
    state="ended",
):
    return viewer.RunSummary(run_id, state, outcome, request, created_at, ended_at)


def running_summary(
    viewer, run_id=7, state="working", request="Fix the bug", created_at=CREATED_DT
):
    return viewer.RunSummary(run_id, state, "", request, created_at, None)


def render_runs(viewer, runs, read_at=ENDED_DT):
    """Render the /runs page for hand-built runs and a fixed read time."""
    return viewer.render_runs_page(viewer.RunsPageData(tuple(runs), read_at))


def _make_runs_table_with_only_an_id(home):
    make_db(home, rows=3)


# Ways for the rows of ``runs`` to be unreadable: the readers that need its columns.
UNREADABLE_ROWS_DATABASES = UNREADABLE_DATABASES + [
    pytest.param(_make_runs_table_with_only_an_id, id="runs_has_only_an_id_column"),
]


def fail_statements_on_runs(sql):
    """An ``after_execute`` hook for ``wrap_connections``: reading ``runs`` fails."""
    if "from runs" in sql.lower():
        raise sqlite3.OperationalError("database is locked")


# ------------------------------------------------------ RunSummary: ended and status


def test_run_summary_ended_is_decided_by_the_outcome_not_the_state(viewer):
    stale_state = ended_summary(viewer, state="working", outcome="rejected")
    assert stale_state.ended is True
    assert stale_state.status == "rejected"

    no_outcome = viewer.RunSummary(7, "ended", "", "Fix the bug", CREATED_DT, ENDED_DT)
    assert no_outcome.ended is False
    assert no_outcome.status == "ended"


# ------------------------------------------------------------ RunSummary: duration


def test_run_summary_duration_of_an_ended_run_ignores_now(viewer):
    run = ended_summary(viewer)
    much_later = ENDED_DT + timedelta(days=400)
    assert run.duration(much_later) == timedelta(seconds=187)
    assert run.duration(CREATED_DT - timedelta(days=1)) == timedelta(seconds=187)


def test_run_summary_duration_of_a_running_run_is_from_created_to_now(viewer):
    run = running_summary(viewer)
    assert run.duration(CREATED_DT + timedelta(hours=2, seconds=7)) == timedelta(
        hours=2, seconds=7
    )


def test_run_summary_duration_is_none_for_an_ended_run_without_created_at(viewer):
    run = ended_summary(viewer, created_at=None)
    assert run.duration(ENDED_DT) is None


def test_run_summary_duration_is_none_for_an_ended_run_without_ended_at(viewer):
    run = ended_summary(viewer, ended_at=None)
    assert run.duration(ENDED_DT + timedelta(days=1)) is None


def test_run_summary_duration_is_zero_when_an_ended_run_ended_before_it_was_created(
    viewer,
):
    run = ended_summary(viewer, ended_at=CREATED_DT - timedelta(seconds=30))
    assert run.duration(ENDED_DT) == timedelta(0)


def test_run_summary_duration_is_zero_when_now_is_before_a_running_run_was_created(
    viewer,
):
    run = running_summary(viewer)
    assert run.duration(CREATED_DT - timedelta(seconds=30)) == timedelta(0)


# ------------------------------------------- read_runs_page_data: readable states


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


# ------------------------------------------------- read_runs_page_data: timestamps


MALFORMED_TIMES = [
    pytest.param("yesterday", id="words"),
    pytest.param("", id="empty_string"),
    pytest.param("2026-11-18 21:04:05", id="space_and_no_z"),
    pytest.param("2026-11-18T21:04:05", id="no_z"),
    pytest.param("2026-11-18T21:04:05+00:00", id="numeric_offset"),
    pytest.param("2026-13-45T99:99:99Z", id="impossible_date"),
    pytest.param("2026-11-18", id="date_only"),
    pytest.param("1763500000", id="epoch_seconds"),
    pytest.param(b"\x00\x01binary", id="blob"),
    pytest.param("2026-11-18T21:04:05.Z", id="a_dot_and_no_fraction"),
    pytest.param("2026-11-18T21:04:05.1234567Z", id="seven_fraction_digits"),
    pytest.param("2026-11-18T21:04:05.123", id="a_fraction_and_no_z"),
    pytest.param("2026-11-18T21:04:05,123Z", id="a_comma_before_the_fraction"),
]


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
FRACTIONAL_TIMES = [
    pytest.param("2026-11-18T21:04:05.250Z", 250000, id="a_quarter_second"),
    pytest.param("2026-11-18T21:04:05.123Z", 123000, id="milliseconds"),
    pytest.param("2026-11-18T21:04:05.000Z", 0, id="zero_milliseconds"),
]


@pytest.mark.parametrize("stored, microsecond", FRACTIONAL_TIMES)
def test_read_runs_page_data_parses_created_at_keeping_the_fraction_of(
    viewer, home, stored, microsecond
):
    make_run_db(home, runs=[{"request": "Fraction", "created_at": stored}])
    (run,) = viewer.read_runs_page_data().runs
    assert run.created_at == CREATED_DT.replace(microsecond=microsecond)
    assert run.created_at.utcoffset() == timedelta(0)
    assert run.ended_at == ENDED_DT


# ------------------------------------------------ read_runs_page_data: empty table


def test_read_runs_page_data_gives_an_empty_tuple_for_an_empty_runs_table(viewer, home):
    make_run_db(home)
    runs = viewer.read_runs_page_data().runs
    assert runs == ()
    assert type(runs) is tuple
    assert runs is not viewer.UNREADABLE


# ------------------------------------------------- read_runs_page_data: unreadable


@pytest.mark.parametrize("break_database", UNREADABLE_ROWS_DATABASES)
def test_read_runs_page_data_runs_is_unreadable_when(viewer, home, break_database):
    break_database(home)
    data = viewer.read_runs_page_data()
    assert isinstance(data, viewer.RunsPageData)
    assert data.runs is viewer.UNREADABLE
    assert data.runs != ()


def test_read_runs_page_data_runs_is_unreadable_when_the_statement_fails(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}])
    proxies = wrap_connections(viewer, monkeypatch, fail_statements_on_runs)

    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


# ------------------------------------------- the readers that give read_at: read_at


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


# --------------------------------------------------- render_runs_page: the document


def test_render_runs_page_with_one_run_is_exactly_the_planned_document(viewer):
    page = render_runs(viewer, [ended_summary(viewer)])
    assert page == (
        RUNS_HEAD
        + RUNS_TABLE_OPEN
        + run_row(7, "accepted", "Fix the bug")
        + RUNS_TABLE_CLOSE
        + DOCUMENT_TAIL
    )


def test_render_runs_page_with_no_runs_is_exactly_the_planned_document(viewer):
    assert render_runs(viewer, []) == RUNS_HEAD + NO_RUNS_LINE + DOCUMENT_TAIL


def test_render_runs_page_with_unreadable_runs_is_exactly_the_planned_document(viewer):
    page = viewer.render_runs_page(viewer.RunsPageData(viewer.UNREADABLE, ENDED_DT))
    assert page == RUNS_HEAD + RUNS_UNAVAILABLE_LINE + DOCUMENT_TAIL


# ------------------------------------------------------- render_runs_page: the rows


def test_render_runs_page_shows_the_exact_row_of_a_running_run_with_so_far(viewer):
    run = running_summary(viewer, run_id=7, request="Add a page at /runs")
    page = render_runs(viewer, [run], read_at=CREATED_DT + timedelta(seconds=187))
    assert (
        run_row(7, "working", "Add a page at /runs", CREATED_TEXT, "3m 7s so far")
        in page
    )


def test_render_runs_page_keeps_rows_in_the_order_given(viewer):
    runs = [
        ended_summary(viewer, run_id=3),
        ended_summary(viewer, run_id=9),
        ended_summary(viewer, run_id=1),
    ]
    page = render_runs(viewer, runs)
    assert re.findall(r'<tr id="run-(\d+)">', page) == ["3", "9", "1"]


# ------------------------------------- render_runs_page: first words of the request


def render_request(viewer, request):
    return render_runs(viewer, [ended_summary(viewer, request=request)])


def test_render_runs_page_shows_a_ten_word_request_in_full_without_an_ellipsis(viewer):
    page = render_request(viewer, TEN_WORDS)
    assert request_cell(TEN_WORDS) in page
    assert "…" not in page


def test_render_runs_page_cuts_an_eleven_word_request_to_ten_words_and_an_ellipsis(
    viewer,
):
    page = render_request(viewer, TEN_WORDS + " eleven")
    assert request_cell(TEN_WORDS + "…") in page
    assert "eleven" not in page


def test_render_runs_page_shows_an_empty_cell_for_a_whitespace_only_request(viewer):
    page = render_request(viewer, " \n\t  \n")
    assert request_cell("") in page
    assert "…" not in page


def test_render_runs_page_collapses_newlines_and_tabs_in_the_request_to_spaces(viewer):
    page = render_request(viewer, "Add a page\n\nat /runs\tlisting\r\nevery   run")
    assert request_cell("Add a page at /runs listing every run") in page


def test_render_runs_page_does_not_add_an_ellipsis_for_trailing_whitespace(viewer):
    page = render_request(viewer, TEN_WORDS + "   \n\n")
    assert request_cell(TEN_WORDS) in page
    assert "…" not in page


# ----------------------------------------------------- render_runs_page: escaping


def test_render_runs_page_escapes_markup_in_a_shortened_request(viewer):
    page = render_request(viewer, "<b>one</b> two three four five six seven eight nine & eleven")
    assert (
        request_cell(
            "&lt;b&gt;one&lt;/b&gt; two three four five six seven eight nine &amp;…"
        )
        in page
    )
    assert "<b>" not in page


def test_render_runs_page_escapes_markup_in_the_state(viewer):
    state = '<i>"working"</i>'
    page = render_runs(viewer, [running_summary(viewer, state=state)])
    assert '<td class="run-status">' + html.escape(state) + "</td>" in page
    assert "<i>" not in page


# ------------------------------------------- render_runs_page: created and duration


DURATIONS = [
    pytest.param(0, "0s", id="zero"),
    pytest.param(1, "1s", id="one_second"),
    pytest.param(42, "42s", id="seconds_only"),
    pytest.param(59, "59s", id="just_under_a_minute"),
    pytest.param(60, "1m 0s", id="exactly_a_minute"),
    pytest.param(187, "3m 7s", id="minutes_and_seconds"),
    pytest.param(3599, "59m 59s", id="just_under_an_hour"),
    pytest.param(3600, "1h 0m 0s", id="exactly_an_hour"),
    pytest.param(7207, "2h 0m 7s", id="hours_with_zero_minutes"),
    pytest.param(86399, "23h 59m 59s", id="just_under_a_day"),
    pytest.param(86400, "1d 0h 0m 0s", id="exactly_a_day"),
    pytest.param(86400 + 187, "1d 0h 3m 7s", id="a_day_with_zero_hours"),
    pytest.param(40 * 86400 + 3661, "40d 1h 1m 1s", id="many_days"),
]


@pytest.mark.parametrize("seconds, text", DURATIONS)
def test_render_runs_page_formats_the_duration_of_an_ended_run_lasting(
    viewer, seconds, text
):
    run = ended_summary(viewer, ended_at=CREATED_DT + timedelta(seconds=seconds))
    page = render_runs(viewer, [run])
    assert duration_cell(text) in page


def test_render_runs_page_shows_the_duration_in_whole_seconds(viewer):
    page = render_runs(
        viewer,
        [running_summary(viewer)],
        read_at=CREATED_DT + timedelta(seconds=187, microseconds=400000),
    )
    assert duration_cell("3m 7s so far") in page


def test_render_runs_page_shows_unknown_created_and_duration_without_created_at(
    viewer,
):
    page = render_runs(viewer, [ended_summary(viewer, created_at=None)])
    assert run_row(7, "accepted", "Fix the bug", "unknown", "unknown") in page


def test_render_runs_page_shows_unknown_duration_for_an_ended_run_without_ended_at(
    viewer,
):
    page = render_runs(
        viewer,
        [ended_summary(viewer, ended_at=None)],
        read_at=ENDED_DT + timedelta(days=2),
    )
    assert run_row(7, "accepted", "Fix the bug", CREATED_TEXT, "unknown") in page
    assert "so far" not in page


def test_render_runs_page_does_not_say_so_far_when_a_running_duration_is_unknown(
    viewer,
):
    page = render_runs(viewer, [running_summary(viewer, created_at=None)])
    assert run_row(7, "working", "Fix the bug", "unknown", "unknown") in page
    assert "so far" not in page


# --------------------------------- render_runs_page does not read database or clock


def test_render_runs_page_does_not_open_the_database_or_look_up_its_path(
    viewer, home, monkeypatch
):
    def forbidden_connect(*args, **kwargs):
        raise AssertionError("render_runs_page must not open the database")

    def forbidden_db_path():
        raise AssertionError("render_runs_page must not look up the database path")

    monkeypatch.setattr(viewer.sqlite3, "connect", forbidden_connect)
    monkeypatch.setattr(viewer, "db_path", forbidden_db_path)

    page = render_runs(viewer, [ended_summary(viewer, request="given by the caller")])
    assert request_cell("given by the caller") in page

    assert NO_RUNS_LINE in render_runs(viewer, [])
    assert RUNS_UNAVAILABLE_LINE in viewer.render_runs_page(
        viewer.RunsPageData(viewer.UNREADABLE, ENDED_DT)
    )


# -------------------------------------------------- render_page: the link to /runs


def test_render_page_is_otherwise_the_same_document_as_before(viewer, home):
    page = viewer.render_page(viewer.PageData(3, None))
    assert page == (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>tomfactory viewer</title>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Hello from the factory</h1>\n"
        + RUNS_LINK_LINE
        + COUNT_LINE.format(3)
        + DOCUMENT_TAIL
    )


# ====================================================================================
# The /runs/<id> page: one run and its steps
# ====================================================================================

# ------------------------------------------------------- helpers for /runs/<id> tests

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

RUN_REQUEST = "Add a page for one run"
RUN_TARGET = "/path/to/target"
RUN_BRANCH = "factory/7-add-a-page"
RUN_REASON = "all checks passed"

# Keyword arguments for ``insert_step`` that make the step one that has not finished.
UNFINISHED = {"passed": None, "ended_at": None, "checks": None}


def checks_json(*checks):
    """The JSON the factory stores for checks given as ``(name, passed)`` pairs."""
    return json.dumps(
        [
            {"name": name, "passed": passed, "note": "", "timed_out": False}
            for name, passed in checks
        ]
    )


def insert_detail_run(
    path,
    run_id=7,
    request=RUN_REQUEST,
    target=RUN_TARGET,
    branch=RUN_BRANCH,
    state="ended",
    outcome="accepted",
    reason=RUN_REASON,
    attempt_limit=3,
    created_at=CREATED,
    ended_at=ENDED,
):
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
    path,
    run_id=7,
    step_id=None,
    name="test",
    kind="agent",
    persona="tester",
    attempt=1,
    checks=None,
    passed=1,
    started_at=CREATED,
    ended_at=ENDED,
):
    """Insert one row into a ``steps`` table; finished and passed by default."""
    execute_sql(
        path,
        "INSERT INTO steps (id, run_id, name, kind, persona, attempt, asked, checks, "
        "passed, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, run_id, name, kind, persona, attempt, "do the work", checks)
        + (passed, started_at, ended_at),
    )


def make_run_db(home, runs=(), steps=(), steps_table=True):
    """Create ``home/factory.db`` with the documented ``runs`` and ``steps`` tables.

    ``runs`` and ``steps`` are sequences of keyword-argument mappings for
    ``insert_detail_run`` and ``insert_step``, inserted in order. With
    ``steps_table=False`` the ``steps`` table is left out.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "factory.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(RUNS_DDL + (STEPS_DDL if steps_table else ""))
        conn.commit()
    finally:
        conn.close()
    for run in runs:
        insert_detail_run(path, **run)
    for step in steps:
        insert_step(path, **step)
    return path


def run_detail(
    viewer,
    run_id=7,
    state="ended",
    outcome="accepted",
    request=RUN_REQUEST,
    created_at=CREATED_DT,
    ended_at=ENDED_DT,
    target=RUN_TARGET,
    branch=RUN_BRANCH,
    reason=RUN_REASON,
    attempt_limit=3,
):
    """A hand-built ``RunDetail``; an ended, accepted run by default."""
    return viewer.RunDetail(
        viewer.RunSummary(run_id, state, outcome, request, created_at, ended_at),
        target,
        branch,
        reason,
        attempt_limit,
    )


def running_detail(viewer, state="working", reason="", **kwargs):
    return run_detail(
        viewer, state=state, outcome="", ended_at=None, reason=reason, **kwargs
    )


def step_summary(
    viewer,
    step_id=12,
    name="test",
    persona="tester",
    attempt=1,
    started_at=CREATED_DT,
    ended_at=ENDED_DT,
    passed=True,
    checks=(),
):
    """A hand-built ``StepSummary``; finished and passed by default."""
    return viewer.StepSummary(
        step_id, name, persona, attempt, started_at, ended_at, passed, checks
    )


def unfinished_step(viewer, **kwargs):
    return step_summary(viewer, passed=None, ended_at=None, **kwargs)


def render_run(viewer, run, steps=(), read_at=ENDED_DT, run_id=7):
    """Render the run page for a hand-built run, steps and a fixed read time."""
    if not isinstance(steps, viewer.Unreadable):
        steps = tuple(steps)
    return viewer.render_run_page(viewer.RunPageData(run_id, run, steps, read_at))


def run_head(title, heading):
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{heading}</h1>\n"
    )


RUNS_HEAD = run_head("Runs - tomfactory viewer", "Runs")
RUN_7_HEAD = run_head("Run 7 - tomfactory viewer", "Run 7")
RUN_NOT_FOUND_HEAD = run_head("Run not found - tomfactory viewer", "Run not found")
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
STEPS_TABLE_CLOSE = "</tbody>\n</table>\n"


def run_not_found_line(run_id):
    return f'<p id="run-not-found">No run with id {run_id}</p>\n'


def run_facts(
    request=RUN_REQUEST,
    target=RUN_TARGET,
    branch=RUN_BRANCH,
    label="Outcome",
    status="accepted",
    reason=RUN_REASON,
    attempt_limit=3,
):
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
    step_id,
    name="test",
    persona="tester",
    attempt=1,
    duration="3m 7s",
    result="passed",
    checks="",
):
    """The table line the run page shows for one step; the texts are already escaped."""
    return (
        f'<tr id="step-{step_id}">'
        f'<td class="step-name">{name}</td>'
        f'<td class="step-persona">{persona}</td>'
        f'<td class="step-attempt">{attempt}</td>'
        f'<td class="step-duration">{duration}</td>'
        f'<td class="step-result">{result}</td>'
        f'<td class="step-checks">{checks}</td>'
        "</tr>\n"
    )


def check_span(name):
    return f'<span class="check">{name}</span>'


def failed_check_span(name):
    return f'<span class="check check-failed">{name} (failed)</span>'


def _make_run_without_steps_table(home):
    make_run_db(home, runs=[{}], steps_table=False)


def fail_statements_on_steps(sql):
    """An ``after_execute`` hook for ``wrap_connections``: reading ``steps`` fails."""
    if "steps" in sql.lower():
        raise sqlite3.OperationalError("database is locked")


# ----------------------------------------------------------- StepSummary: finished


def test_step_summary_finished_is_decided_by_passed_not_by_ended_at(viewer):
    result_without_end = step_summary(viewer, passed=False, ended_at=None)
    assert result_without_end.finished is True

    end_without_result = step_summary(viewer, passed=None, ended_at=ENDED_DT)
    assert end_without_result.finished is False


# ----------------------------------------------------------- StepSummary: duration


def test_step_summary_duration_of_a_finished_step_ignores_now(viewer):
    step = step_summary(viewer)
    assert step.duration(ENDED_DT + timedelta(days=400)) == timedelta(seconds=187)
    assert step.duration(CREATED_DT - timedelta(days=1)) == timedelta(seconds=187)


def test_step_summary_duration_of_an_unfinished_step_is_from_started_to_now(viewer):
    step = unfinished_step(viewer)
    assert step.duration(CREATED_DT + timedelta(hours=2, seconds=7)) == timedelta(
        hours=2, seconds=7
    )


def test_step_summary_duration_of_an_unfinished_step_ignores_a_stray_ended_at(viewer):
    step = step_summary(viewer, passed=None, ended_at=ENDED_DT)
    assert step.duration(CREATED_DT + timedelta(seconds=5)) == timedelta(seconds=5)


def test_step_summary_duration_keeps_fractions_of_a_second(viewer):
    step = step_summary(
        viewer,
        started_at=CREATED_DT.replace(microsecond=123000),
        ended_at=ENDED_DT.replace(microsecond=623000),
    )
    assert step.duration(ENDED_DT) == timedelta(seconds=187, milliseconds=500)


def test_step_summary_duration_is_none_for_a_finished_step_without_started_at(viewer):
    step = step_summary(viewer, started_at=None)
    assert step.duration(ENDED_DT) is None


def test_step_summary_duration_is_none_for_a_finished_step_without_ended_at(viewer):
    step = step_summary(viewer, ended_at=None)
    assert step.duration(ENDED_DT + timedelta(days=1)) is None


def test_step_summary_duration_is_zero_when_a_step_ended_before_it_started(viewer):
    step = step_summary(viewer, ended_at=CREATED_DT - timedelta(seconds=30))
    assert step.duration(ENDED_DT) == timedelta(0)


def test_step_summary_duration_is_zero_when_now_is_before_an_unfinished_step_started(
    viewer,
):
    step = unfinished_step(viewer)
    assert step.duration(CREATED_DT - timedelta(seconds=30)) == timedelta(0)


# ----------------------------------------------------- read_run_page_data: the run


def test_read_run_page_data_maps_every_field_of_a_run_and_its_steps(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {
                "step_id": 11,
                "name": "red",
                "persona": "test-writer",
                "attempt": 1,
                "passed": 1,
                "checks": checks_json(("guard", True)),
            },
            {
                "step_id": 12,
                "name": "test",
                "persona": "tester",
                "attempt": 2,
                "passed": 0,
                "checks": checks_json(("guard", True), ("tests", False)),
            },
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
    assert data.steps == (
        viewer.StepSummary(
            11,
            "red",
            "test-writer",
            1,
            CREATED_DT,
            ENDED_DT,
            True,
            (viewer.Check("guard", True),),
        ),
        viewer.StepSummary(
            12,
            "test",
            "tester",
            2,
            CREATED_DT,
            ENDED_DT,
            False,
            (viewer.Check("guard", True), viewer.Check("tests", False)),
        ),
    )


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
    make_run_db(
        home,
        runs=[{"branch": None, "reason": None}],
        steps=[{"step_id": 12, "persona": None}],
    )
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


# --------------------------------------------------- read_run_page_data: the steps


def test_read_run_page_data_orders_steps_by_id_not_by_insertion_order(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[{"step_id": 30}, {"step_id": 4}, {"step_id": 200}, {"step_id": 12}],
    )
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


def test_read_run_page_data_gives_an_unfinished_step_no_ended_at_and_no_checks(
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


# -------------------------------------------------- read_run_page_data: the checks


def test_read_run_page_data_checks_is_an_empty_tuple_for_an_empty_json_list(
    viewer, home
):
    make_run_db(home, runs=[{}], steps=[{"checks": "[]"}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == ()
    assert step.checks is not viewer.UNREADABLE


def test_read_run_page_data_keeps_checks_in_the_order_they_are_stored(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {
                "checks": checks_json(
                    ("zeta", True), ("alpha", False), ("mid", True), ("alpha", True)
                )
            }
        ],
    )
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == (
        viewer.Check("zeta", True),
        viewer.Check("alpha", False),
        viewer.Check("mid", True),
        viewer.Check("alpha", True),
    )


BAD_CHECKS = [
    pytest.param("not json at all", id="text_that_is_not_json"),
    pytest.param("", id="empty_string"),
    pytest.param('[{"name": "guard", "passed": true}', id="truncated_json"),
    pytest.param('{"name": "guard", "passed": true}', id="an_object_not_a_list"),
    pytest.param('"guard"', id="a_json_string"),
    pytest.param("42", id="a_json_number"),
    pytest.param("null", id="json_null"),
    pytest.param("true", id="json_true"),
    pytest.param('["guard"]', id="an_item_that_is_a_string"),
    pytest.param("[[]]", id="an_item_that_is_a_list"),
    pytest.param("[null]", id="an_item_that_is_null"),
    pytest.param('[{"passed": true}]', id="an_item_missing_name"),
    pytest.param('[{"name": "guard"}]', id="an_item_missing_passed"),
    pytest.param("[{}]", id="an_empty_item"),
    pytest.param('[{"name": 5, "passed": true}]', id="a_name_that_is_a_number"),
    pytest.param('[{"name": null, "passed": true}]', id="a_name_that_is_null"),
    pytest.param('[{"name": "guard", "passed": 1}]', id="passed_that_is_a_number"),
    pytest.param('[{"name": "guard", "passed": "true"}]', id="passed_that_is_text"),
    pytest.param('[{"name": "guard", "passed": null}]', id="passed_that_is_null"),
    pytest.param(
        '[{"name": "guard", "passed": true}, {"name": "tests"}]',
        id="a_good_item_followed_by_a_bad_one",
    ),
    pytest.param(b"\x00\x01binary", id="a_blob"),
]


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


# ---------------------------------------------- read_run_page_data: an unknown id


def test_read_run_page_data_runs_no_statement_on_steps_for_an_unknown_id(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{"run_id": 7}, {"run_id": 99}])
    proxies = wrap_connections(viewer, monkeypatch)

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
    _make_run_without_steps_table(home)
    data = viewer.read_run_page_data(99)
    assert data.run is None
    assert data.steps == ()
    assert data.steps is not viewer.UNREADABLE


# ------------------------------------------------- read_run_page_data: unreadable


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


def test_read_run_page_data_run_and_steps_are_unreadable_when_the_run_statement_fails(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}])
    proxies = wrap_connections(viewer, monkeypatch, fail_statements_on_runs)

    data = viewer.read_run_page_data(7)

    assert data.run is viewer.UNREADABLE
    assert data.steps is viewer.UNREADABLE
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


def test_read_run_page_data_keeps_the_run_and_marks_steps_unreadable_without_a_steps_table(
    viewer, home
):
    _make_run_without_steps_table(home)
    data = viewer.read_run_page_data(7)
    assert data.run == run_detail(viewer)
    assert data.steps is viewer.UNREADABLE
    assert data.steps != ()


def test_read_run_page_data_keeps_the_run_when_the_steps_statement_fails(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}])
    proxies = wrap_connections(viewer, monkeypatch, fail_statements_on_steps)

    data = viewer.read_run_page_data(7)

    assert data.run == run_detail(viewer)
    assert data.steps is viewer.UNREADABLE
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


# -------------------------------------------- read_run_page_data: one moment


def test_read_run_page_data_run_and_steps_come_from_the_same_moment(
    viewer, home, monkeypatch
):
    path = home / "factory.db"
    real_connect = sqlite3.connect
    # Kept open so the database stays in WAL mode with its -wal and -shm files.
    holder = real_connect(path)
    try:
        mode = holder.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode.lower() == "wal"
        holder.executescript(RUNS_DDL + STEPS_DDL)
        holder.commit()
        insert_detail_run(path)
        insert_step(path, name="plan")

        inserted = []

        def insert_second_step_after_the_run_is_read(sql):
            lowered = sql.lower()
            if "runs" not in lowered or "steps" in lowered or inserted:
                return
            inserted.append("attempted")
            writer = real_connect(path, timeout=2)
            try:
                writer.execute(
                    "INSERT INTO steps (run_id, name, kind, attempt, asked, started_at) "
                    "VALUES (7, 'build', 'agent', 1, 'do the work', ?)",
                    (CREATED,),
                )
                writer.commit()
                inserted.append("committed")
            except sqlite3.Error as error:  # reported by the assertion below
                inserted.append(error)
            finally:
                writer.close()

        proxies = wrap_connections(
            viewer, monkeypatch, insert_second_step_after_the_run_is_read
        )

        data = viewer.read_run_page_data(7)

        assert inserted == ["attempted", "committed"], (
            "the second step must be written between the run read and the steps read"
        )
        assert len(proxies) == 1
        assert data.run == run_detail(viewer)
        assert [step.name for step in data.steps] == ["plan"]

        # The second step really was committed: the next read shows it.
        assert [step.name for step in viewer.read_run_page_data(7).steps] == [
            "plan",
            "build",
        ]
    finally:
        holder.close()


# ------------------------------------------------ render_run_page: the documents


def test_render_run_page_for_a_run_with_steps_is_exactly_the_planned_document(viewer):
    check = viewer.Check
    steps = [
        step_summary(
            viewer,
            step_id=11,
            name="red",
            persona="test-writer",
            checks=(check("guard", True),),
        ),
        step_summary(
            viewer,
            step_id=12,
            attempt=2,
            passed=False,
            checks=(check("guard", True), check("tests", False)),
        ),
    ]
    page = render_run(viewer, run_detail(viewer), steps)
    assert page == (
        RUN_7_HEAD
        + run_facts()
        + STEPS_TABLE_OPEN
        + step_row(11, "red", "test-writer", 1, "3m 7s", "passed", check_span("guard"))
        + step_row(
            12,
            "test",
            "tester",
            2,
            "3m 7s",
            "failed",
            check_span("guard") + ", " + failed_check_span("tests"),
        )
        + STEPS_TABLE_CLOSE
        + DOCUMENT_TAIL
    )


def test_render_run_page_for_a_run_with_no_steps_is_exactly_the_planned_document(
    viewer,
):
    page = render_run(viewer, run_detail(viewer), [])
    assert page == RUN_7_HEAD + run_facts() + NO_STEPS_LINE + DOCUMENT_TAIL


def test_render_run_page_for_unreadable_steps_is_exactly_the_planned_document(viewer):
    page = render_run(viewer, run_detail(viewer), viewer.UNREADABLE)
    assert page == RUN_7_HEAD + run_facts() + STEPS_UNAVAILABLE_LINE + DOCUMENT_TAIL


def test_render_run_page_for_an_unreadable_run_is_exactly_the_planned_document(viewer):
    page = render_run(viewer, viewer.UNREADABLE, viewer.UNREADABLE)
    assert page == RUN_7_HEAD + RUN_UNAVAILABLE_LINE + DOCUMENT_TAIL


def test_render_run_page_for_a_run_not_found_is_exactly_the_planned_document(viewer):
    page = render_run(viewer, None, (), run_id=99)
    assert page == RUN_NOT_FOUND_HEAD + run_not_found_line(99) + DOCUMENT_TAIL


# ------------------------------------------------- render_run_page: the run's facts


def test_render_run_page_decides_outcome_or_state_by_the_outcome_not_the_state(viewer):
    stale_state = render_run(viewer, run_detail(viewer, state="working", outcome="rejected"))
    assert '<dt>Outcome</dt><dd id="run-status">rejected</dd>\n' in stale_state

    no_outcome = render_run(viewer, run_detail(viewer, state="ended", outcome=""))
    assert '<dt>State</dt><dd id="run-status">ended</dd>\n' in no_outcome


def test_render_run_page_leaves_out_the_reason_of_an_ended_run_with_an_empty_reason(
    viewer,
):
    page = render_run(viewer, run_detail(viewer, reason=""))
    assert run_facts(reason=None) in page
    assert "run-reason" not in page
    assert "<dt>Reason</dt>" not in page


def test_render_run_page_leaves_out_the_reason_of_a_run_that_has_not_ended(viewer):
    page = render_run(viewer, running_detail(viewer, reason="still going"))
    assert run_facts(label="State", status="working", reason=None) in page
    assert "run-reason" not in page
    assert "still going" not in page


def test_render_run_page_shows_none_for_an_empty_branch(viewer):
    page = render_run(viewer, run_detail(viewer, branch=""))
    assert '<dt>Branch</dt><dd id="run-branch">none</dd>\n' in page


def test_render_run_page_shows_a_long_request_in_full_without_an_ellipsis(viewer):
    request = TEN_WORDS + " eleven twelve thirteen"
    page = render_run(viewer, run_detail(viewer, request=request))
    assert f'<dt>Request</dt><dd id="run-request">{request}</dd>\n' in page
    assert "…" not in page


# ----------------------------------------------------- render_run_page: the steps


def test_render_run_page_shows_the_exact_row_of_an_unfinished_step(viewer):
    step = unfinished_step(viewer, step_id=5, name="document", persona="documenter")
    page = render_run(
        viewer,
        running_detail(viewer),
        [step],
        read_at=CREATED_DT + timedelta(seconds=187),
    )
    assert (
        step_row(5, "document", "documenter", 1, "3m 7s so far", "not finished", "")
        in page
    )


def test_render_run_page_keeps_step_rows_in_the_order_given(viewer):
    steps = [step_summary(viewer, step_id=n) for n in (30, 4, 12)]
    page = render_run(viewer, run_detail(viewer), steps)
    assert re.findall(r'<tr id="step-(\d+)">', page) == ["30", "4", "12"]


# ------------------------------------------- render_run_page: a step's duration


def test_render_run_page_shows_unknown_duration_for_a_finished_step_without_ended_at(
    viewer,
):
    page = render_run(
        viewer,
        run_detail(viewer),
        [step_summary(viewer, passed=False, ended_at=None)],
        read_at=ENDED_DT + timedelta(days=2),
    )
    assert step_row(12, duration="unknown", result="failed") in page
    assert "so far" not in page


def test_render_run_page_does_not_say_so_far_when_an_unfinished_duration_is_unknown(
    viewer,
):
    page = render_run(
        viewer, running_detail(viewer), [unfinished_step(viewer, started_at=None)]
    )
    assert step_row(12, duration="unknown", result="not finished") in page
    assert "so far" not in page


# ---------------------------------------------- render_run_page: a step's checks


def test_render_run_page_shows_other_steps_checks_beside_unreadable_ones(viewer):
    steps = [
        step_summary(viewer, step_id=1, checks=(viewer.Check("guard", True),)),
        step_summary(viewer, step_id=2, checks=viewer.UNREADABLE),
        step_summary(viewer, step_id=3, checks=()),
    ]
    page = render_run(viewer, run_detail(viewer), steps)
    assert step_row(1, checks=check_span("guard")) in page
    assert step_row(2, checks="unreadable") in page
    assert step_row(3, checks="") in page
    assert "steps-unavailable" not in page


# ----------------------------------------------------- render_run_page: escaping


def test_render_run_page_escapes_every_stored_text_at_once(viewer):
    nasty = "<u>&\"'"
    run = run_detail(
        viewer, request=nasty, target=nasty, branch=nasty, outcome=nasty, reason=nasty
    )
    step = step_summary(
        viewer,
        name=nasty,
        persona=nasty,
        passed=False,
        checks=(viewer.Check(nasty, True), viewer.Check(nasty, False)),
    )
    page = render_run(viewer, run, [step])
    assert "<u>" not in page
    assert nasty not in page
    assert page.count(html.escape(nasty)) == 9


# ------------------------------ render_run_page does not read database or clock


def test_render_run_page_does_not_open_the_database_or_look_up_its_path(
    viewer, home, monkeypatch
):
    def forbidden_connect(*args, **kwargs):
        raise AssertionError("render_run_page must not open the database")

    def forbidden_db_path():
        raise AssertionError("render_run_page must not look up the database path")

    monkeypatch.setattr(viewer.sqlite3, "connect", forbidden_connect)
    monkeypatch.setattr(viewer, "db_path", forbidden_db_path)

    page = render_run(
        viewer,
        run_detail(viewer, request="given by the caller"),
        [step_summary(viewer, name="given-step")],
    )
    assert '<dd id="run-request">given by the caller</dd>' in page
    assert '<td class="step-name">given-step</td>' in page

    assert STEPS_UNAVAILABLE_LINE in render_run(
        viewer, run_detail(viewer), viewer.UNREADABLE
    )
    assert RUN_UNAVAILABLE_LINE in render_run(
        viewer, viewer.UNREADABLE, viewer.UNREADABLE
    )
    assert run_not_found_line(7) in render_run(viewer, None, ())


# ------------------------------------------------ HTTP server: every page, joined up

# Each page: its path, and the page the two lower layers give for it.
PAGES = [
    pytest.param("/", lambda v: v.render_page(v.read_page_data()), id="home"),
    pytest.param(
        "/runs", lambda v: v.render_runs_page(v.read_runs_page_data()), id="runs"
    ),
    pytest.param(
        "/runs/7", lambda v: v.render_run_page(v.read_run_page_data(7)), id="run"
    ),
]


@pytest.mark.parametrize("query", ["", "?x=1"], ids=["no_query", "query_string"])
@pytest.mark.parametrize("path, page", PAGES)
def test_get_answers_200_with_the_page_rendered_from_what_was_read(
    viewer, home, port, path, page, query
):
    # Every run and step here has ended, so no page depends on when it was read.
    make_run_db(home, runs=[{"request": "café ✓", "reason": "terminé ✓"}], steps=[{}])
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


# ------------------------------------------- HTTP server, /runs/<id>: an unknown id


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
    assert text == RUN_NOT_FOUND_HEAD + run_not_found_line(99) + DOCUMENT_TAIL
    assert text == viewer.render_run_page(viewer.RunPageData(99, None, (), ENDED_DT))


# --------------------------------------- HTTP server: paths that are not served

NOT_RUN_PAGE_PATHS = [
    pytest.param("/nope", id="an_unknown_path"),
    pytest.param("/index.html", id="a_file_below_the_root"),
    pytest.param("/runs.html", id="runs_with_an_extension"),
    pytest.param("/runs/", id="no_id"),
    pytest.param("/runs/abc", id="letters"),
    pytest.param("/runs/0", id="zero"),
    pytest.param("/runs/07", id="a_leading_zero"),
    pytest.param("/runs/007", id="two_leading_zeros"),
    pytest.param("/runs/7/", id="a_trailing_slash"),
    pytest.param("/runs/1234567890123456789", id="nineteen_digits"),
    pytest.param("/runs/-7", id="a_negative_number"),
    pytest.param("/runs/+7", id="a_plus_sign"),
    pytest.param("/runs/7.0", id="a_decimal_point"),
    pytest.param("/runs/7abc", id="digits_then_letters"),
    pytest.param("/runs/abc7", id="letters_then_digits"),
    pytest.param("/runs/0x7", id="hexadecimal"),
    pytest.param("/runs/7/steps", id="a_path_below_the_run"),
    pytest.param("/runs//7", id="a_doubled_slash"),
    pytest.param("/runs/7/?x=1", id="a_trailing_slash_and_a_query_string"),
    pytest.param("/Runs/7", id="a_capital_letter"),
    pytest.param("/run/7", id="run_in_the_singular"),
    pytest.param("/7", id="the_id_alone"),
]


@pytest.mark.parametrize("path", NOT_RUN_PAGE_PATHS)
def test_get_run_page_is_not_served_and_the_database_is_not_opened_at(
    viewer, home, port, monkeypatch, path
):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{}])
    assert fetch(port, "/runs/7")[0] == 200
    calls = record_connects(viewer, monkeypatch)

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
    calls = record_connects(viewer, monkeypatch)

    status, _, body = fetch(port, "/runs/100000000000000000")
    assert status == 404
    assert run_not_found_line(100000000000000000) in body.decode("utf-8")
    assert_one_read_only_connect(calls)

    status, _, body = fetch(port, "/runs/1000000000000000000")
    assert status == 404
    assert b"run-not-found" not in body
    assert len(calls) == 1


# --------------------------------------- HTTP server, the links from page to page


def test_the_runs_link_on_the_home_page_leads_on_to_the_page_of_a_run(home, port):
    make_run_db(home, runs=[{"request": "reached from the home page"}])
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
