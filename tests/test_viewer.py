"""Tests for the hello-world observability page (``viewer.py`` at the repository root)."""

import ast
import html
import importlib
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
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


def make_db(home, rows=0, with_runs_table=True):
    """Create ``home/factory.db``; optionally with a ``runs`` table holding ``rows`` rows.

    The ``runs`` table made here has no ``outcome`` or ``reason`` column.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "factory.db"
    conn = sqlite3.connect(path)
    try:
        if with_runs_table:
            conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO runs DEFAULT VALUES", [()] * rows)
        else:
            conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    return path


def insert_run(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO runs DEFAULT VALUES")
        conn.commit()
    finally:
        conn.close()


def make_outcome_db(home, runs=()):
    """Create ``home/factory.db`` whose ``runs`` table has outcome and reason columns.

    ``runs`` is a sequence of ``(outcome, reason)`` pairs, inserted in order.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "factory.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, outcome TEXT, reason TEXT)"
        )
        conn.executemany("INSERT INTO runs (outcome, reason) VALUES (?, ?)", list(runs))
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


def insert_outcome_run(path, outcome, reason, run_id=None):
    execute_sql(
        path,
        "INSERT INTO runs (id, outcome, reason) VALUES (?, ?, ?)",
        (run_id, outcome, reason),
    )


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
    pytest.param(_write_empty_file, id="database_file_is_empty"),
    pytest.param(_write_non_sqlite_file, id="file_is_not_a_sqlite_database"),
    pytest.param(_make_factory_db_a_directory, id="factory_db_is_a_directory"),
    pytest.param(_make_database_without_runs_table, id="runs_table_is_missing"),
]


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


def test_db_path_returns_a_path_object(viewer, home):
    assert isinstance(viewer.db_path(), Path)


def test_db_path_reads_the_environment_on_every_call(viewer, tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("TOMFACTORY_HOME", str(first))
    assert viewer.db_path() == first / "factory.db"
    monkeypatch.setenv("TOMFACTORY_HOME", str(second))
    assert viewer.db_path() == second / "factory.db"


# --------------------------------------------------- the removed one-fact-per-call API


@pytest.mark.parametrize("name", ["count_runs", "latest_run", "_fetch_one"])
def test_viewer_no_longer_has_the_one_connection_per_fact_readers(viewer, name):
    assert not hasattr(viewer, name)


# ---------------------------------------------------------- UNREADABLE and PageData


def test_unreadable_is_the_single_member_of_the_unreadable_enum(viewer):
    assert viewer.UNREADABLE is viewer.Unreadable.UNREADABLE
    assert list(viewer.Unreadable) == [viewer.UNREADABLE]
    assert isinstance(viewer.UNREADABLE, viewer.Unreadable)


def test_unreadable_is_not_none_and_not_equal_to_a_count_or_a_run(viewer):
    assert viewer.UNREADABLE is not None
    assert viewer.UNREADABLE != 0
    assert viewer.UNREADABLE != ""
    assert viewer.UNREADABLE != viewer.LatestRun("", "")


def test_page_data_has_count_and_latest_fields_in_that_order(viewer):
    data = viewer.PageData(3, viewer.LatestRun("passed", "all green"))
    assert viewer.PageData._fields == ("count", "latest")
    assert data.count == 3
    assert data.latest == viewer.LatestRun("passed", "all green")


def test_page_data_accepts_its_fields_by_keyword(viewer):
    assert viewer.PageData(count=0, latest=None) == viewer.PageData(0, None)


def test_page_data_requires_latest_to_be_given(viewer):
    page_data = viewer.PageData
    with pytest.raises(TypeError):
        page_data(3)


def test_page_data_requires_count_to_be_given(viewer):
    page_data = viewer.PageData
    with pytest.raises(TypeError):
        page_data(latest=None)


# --------------------------------------------------- read_page_data: readable states


def test_read_page_data_returns_a_page_data(viewer, home):
    make_outcome_db(home, [("passed", "all green")])
    assert isinstance(viewer.read_page_data(), viewer.PageData)


def test_read_page_data_returns_count_and_latest_run_of_a_single_ended_run(viewer, home):
    make_outcome_db(home, [("passed", "all green")])
    assert viewer.read_page_data() == viewer.PageData(
        1, viewer.LatestRun("passed", "all green")
    )


def test_read_page_data_latest_is_a_latest_run_with_outcome_and_reason(viewer, home):
    make_outcome_db(home, [("failed", "tests red")])
    latest = viewer.read_page_data().latest
    assert isinstance(latest, viewer.LatestRun)
    assert latest.outcome == "failed"
    assert latest.reason == "tests red"


def test_read_page_data_counts_every_row_and_returns_the_row_with_the_highest_id(
    viewer, home
):
    make_outcome_db(
        home, [("passed", "first"), ("failed", "second"), ("passed", "third")]
    )
    assert viewer.read_page_data() == viewer.PageData(
        3, viewer.LatestRun("passed", "third")
    )


def test_read_page_data_latest_uses_highest_id_not_insertion_order(viewer, home):
    path = make_outcome_db(home)
    insert_outcome_run(path, "failed", "newest by id", run_id=50)
    insert_outcome_run(path, "passed", "inserted last", run_id=7)
    assert viewer.read_page_data() == viewer.PageData(
        2, viewer.LatestRun("failed", "newest by id")
    )


def test_read_page_data_count_is_the_number_of_rows_not_the_highest_id(viewer, home):
    path = make_outcome_db(home)
    insert_outcome_run(path, "passed", "all green", run_id=50)
    assert viewer.read_page_data().count == 1


def test_read_page_data_latest_is_the_not_ended_row_when_older_rows_have_ended(
    viewer, home
):
    make_outcome_db(home, [("passed", "all green"), ("failed", "boom"), (None, None)])
    assert viewer.read_page_data() == viewer.PageData(3, viewer.LatestRun("", ""))


def test_read_page_data_latest_ignores_older_not_ended_rows(viewer, home):
    make_outcome_db(home, [(None, None), ("", ""), ("passed", "all green")])
    assert viewer.read_page_data() == viewer.PageData(
        3, viewer.LatestRun("passed", "all green")
    )


def test_read_page_data_latest_has_empty_strings_for_null_columns(viewer, home):
    make_outcome_db(home, [(None, None)])
    data = viewer.read_page_data()
    assert data.latest is not None
    assert data.latest is not viewer.UNREADABLE
    assert data == viewer.PageData(1, viewer.LatestRun("", ""))
    assert data.latest.outcome == ""
    assert data.latest.reason == ""


def test_read_page_data_latest_has_empty_strings_for_empty_string_columns(viewer, home):
    make_outcome_db(home, [("", "")])
    data = viewer.read_page_data()
    assert data.latest is not None
    assert data.latest is not viewer.UNREADABLE
    assert data == viewer.PageData(1, viewer.LatestRun("", ""))


def test_read_page_data_latest_has_empty_reason_when_ended_run_has_null_reason(
    viewer, home
):
    make_outcome_db(home, [("passed", None)])
    assert viewer.read_page_data().latest == viewer.LatestRun("passed", "")


def test_read_page_data_latest_keeps_reason_when_outcome_is_null(viewer, home):
    make_outcome_db(home, [(None, "still going")])
    assert viewer.read_page_data().latest == viewer.LatestRun("", "still going")


def test_read_page_data_returns_text_unchanged_including_markup_and_unicode(
    viewer, home
):
    make_outcome_db(home, [("<b>&", "café ✓ 'quoted'")])
    assert viewer.read_page_data().latest == viewer.LatestRun(
        "<b>&", "café ✓ 'quoted'"
    )


def test_read_page_data_count_is_an_int_when_readable(viewer, home):
    make_outcome_db(home, [("passed", "a"), (None, None)])
    count = viewer.read_page_data().count
    assert type(count) is int
    assert count == 2


def test_read_page_data_works_when_home_path_contains_a_space(
    viewer, tmp_path, monkeypatch
):
    spaced = tmp_path / "factory home"
    make_outcome_db(spaced, [("passed", "all green"), ("failed", "tests red")])
    monkeypatch.setenv("TOMFACTORY_HOME", str(spaced))
    assert viewer.read_page_data() == viewer.PageData(
        2, viewer.LatestRun("failed", "tests red")
    )


# ------------------------------------------------- read_page_data: empty runs table


def test_read_page_data_returns_zero_and_none_for_an_empty_runs_table(viewer, home):
    make_outcome_db(home, [])
    assert viewer.read_page_data() == viewer.PageData(0, None)


def test_read_page_data_count_is_an_int_zero_for_an_empty_runs_table(viewer, home):
    make_outcome_db(home, [])
    count = viewer.read_page_data().count
    assert type(count) is int
    assert count == 0
    assert count is not viewer.UNREADABLE


def test_read_page_data_latest_is_none_not_unreadable_for_an_empty_runs_table(
    viewer, home
):
    make_outcome_db(home, [])
    latest = viewer.read_page_data().latest
    assert latest is None
    assert latest is not viewer.UNREADABLE


# ----------------------------------------- read_page_data: outcome columns unreadable


def test_read_page_data_keeps_the_count_when_runs_has_no_outcome_or_reason_columns(
    viewer, home
):
    make_db(home, rows=3)
    assert viewer.read_page_data() == viewer.PageData(3, viewer.UNREADABLE)


def test_read_page_data_latest_is_unreadable_not_none_when_columns_are_missing(
    viewer, home
):
    make_db(home, rows=3)
    latest = viewer.read_page_data().latest
    assert latest is viewer.UNREADABLE
    assert latest is not None


def test_read_page_data_count_is_an_int_when_only_the_latest_run_is_unreadable(
    viewer, home
):
    make_db(home, rows=3)
    count = viewer.read_page_data().count
    assert type(count) is int
    assert count == 3


def test_read_page_data_latest_is_unreadable_when_runs_has_outcome_but_no_reason(
    viewer, home
):
    path = make_db(home, with_runs_table=False)
    execute_sql(path, "CREATE TABLE runs (id INTEGER PRIMARY KEY, outcome TEXT)")
    execute_sql(path, "INSERT INTO runs (outcome) VALUES ('passed')")
    assert viewer.read_page_data() == viewer.PageData(1, viewer.UNREADABLE)


def test_read_page_data_latest_is_unreadable_when_runs_has_reason_but_no_outcome(
    viewer, home
):
    path = make_db(home, with_runs_table=False)
    execute_sql(path, "CREATE TABLE runs (id INTEGER PRIMARY KEY, reason TEXT)")
    execute_sql(path, "INSERT INTO runs (reason) VALUES ('all green')")
    assert viewer.read_page_data() == viewer.PageData(1, viewer.UNREADABLE)


def test_read_page_data_latest_is_unreadable_for_an_empty_table_without_the_columns(
    viewer, home
):
    # No rows and no columns: "could not read" wins over "no runs" for the latest run,
    # while the count is still a readable zero.
    make_db(home, rows=0)
    data = viewer.read_page_data()
    assert data == viewer.PageData(0, viewer.UNREADABLE)
    assert data.latest is not None


# ------------------------------------------------ read_page_data: database unreadable


@pytest.mark.parametrize("break_database", UNREADABLE_DATABASES)
def test_read_page_data_marks_both_facts_unreadable_when(viewer, home, break_database):
    break_database(home)
    data = viewer.read_page_data()
    assert data == viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE)
    assert data.count is viewer.UNREADABLE
    assert data.latest is viewer.UNREADABLE


@pytest.mark.parametrize("break_database", UNREADABLE_DATABASES)
def test_read_page_data_never_reports_no_runs_when(viewer, home, break_database):
    break_database(home)
    data = viewer.read_page_data()
    assert data.count is not None
    assert data.count != 0
    assert data.latest is not None


def test_read_page_data_marks_both_facts_unreadable_when_home_directory_is_missing(
    viewer, tmp_path, monkeypatch
):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("TOMFACTORY_HOME", str(missing))
    assert viewer.read_page_data() == viewer.PageData(
        viewer.UNREADABLE, viewer.UNREADABLE
    )


def test_read_page_data_marks_both_facts_unreadable_when_connect_raises(
    viewer, home, monkeypatch
):
    make_outcome_db(home, [("passed", "all green")])

    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(viewer.sqlite3, "connect", failing_connect)
    assert viewer.read_page_data() == viewer.PageData(
        viewer.UNREADABLE, viewer.UNREADABLE
    )


# ------------------------------------------- read_page_data: one read-only connection


def test_read_page_data_opens_one_read_only_connection_for_count_and_latest_run(
    viewer, home, monkeypatch
):
    make_outcome_db(home, [("passed", "all green"), ("failed", "tests red")])
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_page_data() == viewer.PageData(
        2, viewer.LatestRun("failed", "tests red")
    )
    assert_one_read_only_connect(calls)


def test_read_page_data_opens_one_read_only_connection_for_an_empty_runs_table(
    viewer, home, monkeypatch
):
    make_outcome_db(home, [])
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_page_data() == viewer.PageData(0, None)
    assert_one_read_only_connect(calls)


def test_read_page_data_opens_one_read_only_connection_when_columns_are_missing(
    viewer, home, monkeypatch
):
    make_db(home, rows=3)
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_page_data() == viewer.PageData(3, viewer.UNREADABLE)
    assert_one_read_only_connect(calls)


def test_read_page_data_tries_to_connect_only_once_when_the_database_is_missing(
    viewer, home, monkeypatch
):
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_page_data() == viewer.PageData(
        viewer.UNREADABLE, viewer.UNREADABLE
    )
    assert_one_read_only_connect(calls)


def test_read_page_data_opens_one_connection_per_call(viewer, home, monkeypatch):
    make_outcome_db(home, [("passed", "all green")])
    calls = record_connects(viewer, monkeypatch)

    viewer.read_page_data()
    viewer.read_page_data()
    assert len(calls) == 2


def test_read_page_data_closes_its_connection_once_after_a_successful_read(
    viewer, home, monkeypatch
):
    make_outcome_db(home, [("passed", "all green")])
    proxies = wrap_connections(viewer, monkeypatch)

    assert viewer.read_page_data() == viewer.PageData(
        1, viewer.LatestRun("passed", "all green")
    )
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


def test_read_page_data_closes_its_connection_when_the_latest_run_is_unreadable(
    viewer, home, monkeypatch
):
    make_db(home, rows=3)
    proxies = wrap_connections(viewer, monkeypatch)

    assert viewer.read_page_data() == viewer.PageData(3, viewer.UNREADABLE)
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


def test_read_page_data_closes_its_connection_when_nothing_is_readable(
    viewer, home, monkeypatch
):
    make_db(home, with_runs_table=False)
    proxies = wrap_connections(viewer, monkeypatch)

    assert viewer.read_page_data() == viewer.PageData(
        viewer.UNREADABLE, viewer.UNREADABLE
    )
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


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


# -------------------------------------------- read_page_data: never writes or creates


def test_read_page_data_does_not_create_a_missing_database_file(viewer, home):
    viewer.read_page_data()
    viewer.read_page_data()
    assert not (home / "factory.db").exists()
    assert list(home.iterdir()) == []


def test_read_page_data_does_not_create_a_missing_home_directory(
    viewer, tmp_path, monkeypatch
):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("TOMFACTORY_HOME", str(missing))
    viewer.read_page_data()
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_read_page_data_leaves_the_database_file_unchanged(viewer, home):
    path = make_outcome_db(home, [("passed", "all green"), (None, None)])
    before = path.read_bytes()
    viewer.read_page_data()
    viewer.read_page_data()
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_page_data_leaves_an_empty_runs_table_unchanged(viewer, home):
    path = make_outcome_db(home, [])
    before = path.read_bytes()
    assert viewer.read_page_data() == viewer.PageData(0, None)
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_page_data_leaves_a_database_without_outcome_columns_unchanged(
    viewer, home
):
    path = make_db(home, rows=3)
    before = path.read_bytes()
    assert viewer.read_page_data() == viewer.PageData(3, viewer.UNREADABLE)
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_page_data_leaves_a_database_without_runs_table_unchanged(viewer, home):
    path = make_db(home, with_runs_table=False)
    before = path.read_bytes()
    viewer.read_page_data()
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_page_data_leaves_an_empty_database_file_empty(viewer, home):
    path = home / "factory.db"
    path.write_bytes(b"")
    viewer.read_page_data()
    assert path.read_bytes() == b""
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_page_data_leaves_a_non_sqlite_file_unchanged(viewer, home):
    path = home / "factory.db"
    content = b"this is definitely not a sqlite database\n" * 50
    path.write_bytes(content)
    viewer.read_page_data()
    assert path.read_bytes() == content
    assert [p.name for p in home.iterdir()] == ["factory.db"]


# ----------------------------------------------------- read_page_data: between calls


def test_read_page_data_called_twice_returns_the_same_value(viewer, home):
    make_outcome_db(home, [("passed", "all green")])
    first = viewer.read_page_data()
    second = viewer.read_page_data()
    assert first == second == viewer.PageData(
        1, viewer.LatestRun("passed", "all green")
    )


def test_read_page_data_sees_a_newly_inserted_row_on_the_next_call(viewer, home):
    path = make_outcome_db(home, [("passed", "all green")])
    assert viewer.read_page_data() == viewer.PageData(
        1, viewer.LatestRun("passed", "all green")
    )
    insert_outcome_run(path, "failed", "lint errors")
    assert viewer.read_page_data() == viewer.PageData(
        2, viewer.LatestRun("failed", "lint errors")
    )


def test_read_page_data_sees_the_latest_row_ending_on_the_next_call(viewer, home):
    path = make_outcome_db(home, [(None, None)])
    assert viewer.read_page_data() == viewer.PageData(1, viewer.LatestRun("", ""))
    execute_sql(path, "UPDATE runs SET outcome = 'passed', reason = 'all green'")
    assert viewer.read_page_data() == viewer.PageData(
        1, viewer.LatestRun("passed", "all green")
    )


def test_read_page_data_sees_the_first_row_inserted_into_an_empty_table(viewer, home):
    path = make_outcome_db(home, [])
    assert viewer.read_page_data() == viewer.PageData(0, None)
    insert_outcome_run(path, "passed", "all green")
    assert viewer.read_page_data() == viewer.PageData(
        1, viewer.LatestRun("passed", "all green")
    )


def test_read_page_data_recovers_once_the_database_appears(viewer, home):
    assert viewer.read_page_data() == viewer.PageData(
        viewer.UNREADABLE, viewer.UNREADABLE
    )
    make_outcome_db(home, [("passed", "all green")])
    assert viewer.read_page_data() == viewer.PageData(
        1, viewer.LatestRun("passed", "all green")
    )


def test_read_page_data_latest_becomes_readable_once_the_columns_are_added(
    viewer, home
):
    path = make_db(home, rows=2)
    assert viewer.read_page_data() == viewer.PageData(2, viewer.UNREADABLE)
    execute_sql(path, "ALTER TABLE runs ADD COLUMN outcome TEXT")
    execute_sql(path, "ALTER TABLE runs ADD COLUMN reason TEXT")
    assert viewer.read_page_data() == viewer.PageData(2, viewer.LatestRun("", ""))


def test_read_page_data_reads_the_environment_on_every_call(
    viewer, tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    make_outcome_db(first, [("passed", "from first")])
    make_outcome_db(second, [("failed", "a"), ("failed", "from second")])
    monkeypatch.setenv("TOMFACTORY_HOME", str(first))
    assert viewer.read_page_data() == viewer.PageData(
        1, viewer.LatestRun("passed", "from first")
    )
    monkeypatch.setenv("TOMFACTORY_HOME", str(second))
    assert viewer.read_page_data() == viewer.PageData(
        2, viewer.LatestRun("failed", "from second")
    )


# ------------------------------------------------------- render_page: the count line


def test_render_page_says_hello(viewer, home):
    page = viewer.render_page(viewer.PageData(3, None))
    assert "<h1>Hello from the factory</h1>" in page


def test_render_page_is_a_complete_html_document_with_title(viewer, home):
    page = viewer.render_page(viewer.PageData(3, None))
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert "<title>tomfactory viewer</title>" in page
    assert page.endswith("</body>\n</html>\n")


def test_render_page_shows_the_run_count(viewer, home):
    page = viewer.render_page(viewer.PageData(3, None))
    assert '<p>Runs recorded: <strong id="run-count">3</strong></p>' in page
    assert "run-count-unavailable" not in page


def test_render_page_shows_zero_as_a_count_not_as_unavailable(viewer, home):
    page = viewer.render_page(viewer.PageData(0, None))
    assert '<strong id="run-count">0</strong>' in page
    assert "run-count-unavailable" not in page


def test_render_page_shows_a_large_count_in_full(viewer, home):
    page = viewer.render_page(viewer.PageData(1234567, None))
    assert '<strong id="run-count">1234567</strong>' in page


def test_render_page_shows_count_unavailable_with_database_path_for_unreadable_count(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, None))
    assert count_unavailable_line(home / "factory.db") in page
    assert "<h1>Hello from the factory</h1>" in page
    assert 'id="run-count"' not in page
    assert "Runs recorded" not in page


def test_render_page_escapes_html_in_the_database_path(viewer, tmp_path, monkeypatch):
    nasty = tmp_path / "a&b<i>c"
    monkeypatch.setenv("TOMFACTORY_HOME", str(nasty))
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, None))
    assert html.escape(str(nasty / "factory.db")) in page
    assert "<i>" not in page
    assert "a&b" not in page


def test_render_page_takes_one_page_data_and_no_other_argument(viewer, home):
    data = viewer.PageData(3, None)
    with pytest.raises(TypeError):
        viewer.render_page(data, None)
    with pytest.raises(TypeError):
        viewer.render_page(data, latest=viewer.LatestRun("passed", "all green"))


def test_render_page_called_twice_with_the_same_data_returns_the_same_page(
    viewer, home
):
    data = viewer.PageData(3, viewer.UNREADABLE)
    assert viewer.render_page(data) == viewer.render_page(data)


# ------------------------------------------------- render_page: no runs (latest None)


def test_render_page_with_no_latest_run_has_no_latest_run_text(viewer, home):
    page = viewer.render_page(viewer.PageData(3, None))
    assert "latest-run" not in page
    assert "Latest run" not in page


def test_render_page_with_no_latest_run_adds_nothing_after_the_count_line(viewer, home):
    page = viewer.render_page(viewer.PageData(3, None))
    assert page.endswith(COUNT_LINE.format(3) + "</body>\n</html>\n")


def test_render_page_with_zero_runs_and_no_latest_run_says_only_the_count(viewer, home):
    page = viewer.render_page(viewer.PageData(0, None))
    assert page.endswith(COUNT_LINE.format(0) + "</body>\n</html>\n")
    assert "unavailable" not in page


def test_render_page_with_unreadable_count_and_no_latest_run_has_no_latest_run_text(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, None))
    assert 'id="run-count-unavailable"' in page
    assert "latest-run" not in page


# ---------------------------------------- render_page: latest run could not be read


def test_render_page_says_latest_run_unavailable_when_latest_is_unreadable(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))
    assert LATEST_UNAVAILABLE_LINE in page


def test_render_page_puts_latest_run_unavailable_right_after_the_count_line(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))
    assert page.endswith(
        COUNT_LINE.format(3) + LATEST_UNAVAILABLE_LINE + "</body>\n</html>\n"
    )


def test_render_page_with_unreadable_latest_shows_no_outcome_reason_or_not_ended(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))
    assert "latest-run-outcome" not in page
    assert "latest-run-reason" not in page
    assert "latest-run-not-ended" not in page


def test_render_page_with_unreadable_latest_still_shows_the_count(viewer, home):
    page = viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))
    assert '<p>Runs recorded: <strong id="run-count">3</strong></p>' in page
    assert "run-count-unavailable" not in page


def test_render_page_shows_latest_run_unavailable_exactly_once(viewer, home):
    page = viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))
    assert page.count("latest-run-unavailable") == 1


def test_render_page_with_unreadable_latest_only_adds_the_unavailable_line(
    viewer, home
):
    base = viewer.render_page(viewer.PageData(3, None))
    page = viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))
    assert page != base
    assert page.replace(LATEST_UNAVAILABLE_LINE, "") == base


def test_render_page_differs_between_no_runs_and_an_unreadable_latest_run(viewer, home):
    empty = viewer.render_page(viewer.PageData(0, None))
    unreadable = viewer.render_page(viewer.PageData(0, viewer.UNREADABLE))
    assert empty != unreadable
    assert "latest-run-unavailable" not in empty
    assert LATEST_UNAVAILABLE_LINE in unreadable
    assert '<strong id="run-count">0</strong>' in unreadable


def test_render_page_shows_both_unavailable_lines_when_nothing_could_be_read(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE))
    assert count_unavailable_line(home / "factory.db") in page
    assert LATEST_UNAVAILABLE_LINE in page
    assert 'id="run-count"' not in page
    assert "<h1>Hello from the factory</h1>" in page


def test_render_page_puts_count_unavailable_before_latest_run_unavailable(viewer, home):
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE))
    assert page.endswith(
        count_unavailable_line(home / "factory.db")
        + LATEST_UNAVAILABLE_LINE
        + "</body>\n</html>\n"
    )


# ------------------------------------------------------ render_page: a latest run


def test_render_page_shows_outcome_and_reason_of_an_ended_run(viewer, home):
    page = viewer.render_page(
        viewer.PageData(3, viewer.LatestRun("passed", "all green"))
    )
    assert OUTCOME_LINE.format("passed") in page
    assert REASON_LINE.format("all green") in page
    assert "latest-run-not-ended" not in page
    assert "latest-run-unavailable" not in page


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


def test_render_page_with_ended_run_only_adds_the_two_latest_run_lines(viewer, home):
    base = viewer.render_page(viewer.PageData(3, None))
    page = viewer.render_page(
        viewer.PageData(3, viewer.LatestRun("passed", "all green"))
    )
    added = OUTCOME_LINE.format("passed") + REASON_LINE.format("all green")
    assert page.replace(added, "") == base
    assert page != base


def test_render_page_omits_reason_line_when_ended_run_has_empty_reason(viewer, home):
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("passed", "")))
    assert OUTCOME_LINE.format("passed") in page
    assert "latest-run-reason" not in page
    assert "Latest run reason" not in page
    assert "latest-run-not-ended" not in page


def test_render_page_shows_not_ended_line_for_a_run_with_empty_outcome(viewer, home):
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("", "")))
    assert NOT_ENDED_LINE in page
    assert "latest-run-outcome" not in page
    assert "latest-run-reason" not in page
    assert "latest-run-unavailable" not in page


def test_render_page_puts_not_ended_line_right_after_the_count_line(viewer, home):
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("", "")))
    expected = COUNT_LINE.format(3) + NOT_ENDED_LINE + "</body>\n</html>\n"
    assert page.endswith(expected)


def test_render_page_shows_not_ended_and_hides_reason_when_outcome_is_empty(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("", "still going")))
    assert NOT_ENDED_LINE in page
    assert "latest-run-outcome" not in page
    assert "latest-run-reason" not in page
    assert "still going" not in page


def test_render_page_shows_not_ended_line_exactly_once(viewer, home):
    page = viewer.render_page(viewer.PageData(1, viewer.LatestRun("", "")))
    assert page.count("latest-run-not-ended") == 1


def test_render_page_shows_outcome_and_reason_elements_exactly_once(viewer, home):
    page = viewer.render_page(
        viewer.PageData(1, viewer.LatestRun("passed", "all green"))
    )
    assert page.count('id="latest-run-outcome"') == 1
    assert page.count('id="latest-run-reason"') == 1


def test_render_page_escapes_html_in_the_outcome(viewer, home):
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("<b>&", "fine")))
    assert OUTCOME_LINE.format("&lt;b&gt;&amp;") in page
    assert "<b>" not in page
    assert "<b>&" not in page


def test_render_page_escapes_html_in_the_reason(viewer, home):
    page = viewer.render_page(
        viewer.PageData(
            3, viewer.LatestRun("failed", "<script>alert(1)</script> & more")
        )
    )
    assert (
        REASON_LINE.format("&lt;script&gt;alert(1)&lt;/script&gt; &amp; more") in page
    )
    assert "<script>" not in page


def test_render_page_escapes_quotes_in_outcome_and_reason(viewer, home):
    page = viewer.render_page(
        viewer.PageData(3, viewer.LatestRun('say "hi"', "it's \"bad\""))
    )
    assert OUTCOME_LINE.format(html.escape('say "hi"')) in page
    assert REASON_LINE.format(html.escape("it's \"bad\"")) in page
    assert 'say "hi"' not in page


def test_render_page_keeps_the_count_line_alongside_an_ended_run(viewer, home):
    page = viewer.render_page(
        viewer.PageData(3, viewer.LatestRun("passed", "all green"))
    )
    assert '<p>Runs recorded: <strong id="run-count">3</strong></p>' in page
    assert "<h1>Hello from the factory</h1>" in page
    assert "latest-run-outcome" in page


def test_render_page_keeps_the_count_line_alongside_a_not_ended_run(viewer, home):
    page = viewer.render_page(viewer.PageData(1, viewer.LatestRun("", "")))
    assert '<p>Runs recorded: <strong id="run-count">1</strong></p>' in page
    assert 'id="latest-run-not-ended"' in page


def test_render_page_shows_latest_run_even_when_count_is_unreadable(viewer, home):
    page = viewer.render_page(
        viewer.PageData(viewer.UNREADABLE, viewer.LatestRun("passed", "all green"))
    )
    assert 'id="run-count-unavailable"' in page
    assert OUTCOME_LINE.format("passed") in page
    assert REASON_LINE.format("all green") in page
    assert "latest-run-unavailable" not in page


def test_render_page_shows_multiline_reason_in_full(viewer, home):
    reason = "line one\nline two"
    page = viewer.render_page(viewer.PageData(3, viewer.LatestRun("failed", reason)))
    assert REASON_LINE.format(reason) in page


# ------------------------------------------- render_page does not read the database


def test_render_page_does_not_open_the_database_for_a_latest_run(
    viewer, home, monkeypatch
):
    make_outcome_db(home, [("failed", "from the database")])

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("render_page must not open the database")

    monkeypatch.setattr(viewer.sqlite3, "connect", forbidden_connect)

    page = viewer.render_page(
        viewer.PageData(1, viewer.LatestRun("passed", "given by the caller"))
    )
    assert OUTCOME_LINE.format("passed") in page
    assert REASON_LINE.format("given by the caller") in page
    assert "from the database" not in page


def test_render_page_does_not_open_the_database_when_told_nothing_was_readable(
    viewer, home, monkeypatch
):
    make_outcome_db(home, [("failed", "from the database")])

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("render_page must not open the database")

    monkeypatch.setattr(viewer.sqlite3, "connect", forbidden_connect)

    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE))
    assert 'id="run-count-unavailable"' in page
    assert LATEST_UNAVAILABLE_LINE in page
    assert 'id="run-count"' not in page
    assert "from the database" not in page


def test_render_page_with_no_latest_run_ignores_runs_in_the_database(viewer, home):
    make_outcome_db(home, [("failed", "from the database")])
    page = viewer.render_page(viewer.PageData(1, None))
    assert "latest-run" not in page
    assert "from the database" not in page


# ----------------------------------------------------------------------- HTTP server


def test_get_root_returns_200_html_with_hello_and_run_count(home, port):
    make_db(home, rows=3)
    status, headers, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "Hello from the factory" in text
    assert '<strong id="run-count">3</strong>' in text


def test_get_root_declares_utf8_charset(home, port):
    make_db(home, rows=3)
    _, headers, _ = fetch(port)
    assert headers["Content-Type"].replace(" ", "").lower() == "text/html;charset=utf-8"


def test_get_root_content_length_matches_body(home, port):
    make_db(home, rows=3)
    _, headers, body = fetch(port)
    assert headers["Content-Length"] is not None
    assert int(headers["Content-Length"]) == len(body)
    assert len(body) > 0


def test_get_root_body_is_the_page_rendered_from_the_page_data(viewer, home, port):
    make_outcome_db(home, [("failed", "old"), ("passed", "all green")])
    _, _, body = fetch(port)
    assert body.decode("utf-8") == viewer.render_page(viewer.read_page_data())
    assert b"latest-run-outcome" in body


def test_get_root_opens_one_read_only_connection_per_request(
    viewer, home, port, monkeypatch
):
    make_outcome_db(home, [("passed", "all green"), ("failed", "tests red")])
    calls = record_connects(viewer, monkeypatch)

    status, _, body = fetch(port)
    text = body.decode("utf-8")

    assert status == 200
    assert '<strong id="run-count">2</strong>' in text
    assert OUTCOME_LINE.format("failed") in text
    assert_one_read_only_connect(calls)


def test_get_root_opens_one_read_only_connection_when_the_database_is_missing(
    viewer, home, port, monkeypatch
):
    calls = record_connects(viewer, monkeypatch)

    status, _, body = fetch(port)

    assert status == 200
    assert LATEST_UNAVAILABLE_LINE in body.decode("utf-8")
    assert_one_read_only_connect(calls)


def test_get_root_shows_zero_for_empty_runs_table(home, port):
    make_db(home, rows=0)
    status, _, body = fetch(port)
    assert status == 200
    assert '<strong id="run-count">0</strong>' in body.decode("utf-8")


def test_get_root_shows_new_count_after_row_inserted_while_serving(home, port):
    path = make_db(home, rows=3)
    _, _, first = fetch(port)
    assert '<strong id="run-count">3</strong>' in first.decode("utf-8")

    insert_run(path)

    status, _, second = fetch(port)
    assert status == 200
    assert '<strong id="run-count">4</strong>' in second.decode("utf-8")


def test_get_root_twice_returns_the_same_page(home, port):
    make_db(home, rows=3)
    first = fetch(port)
    second = fetch(port)
    assert first[0] == second[0] == 200
    assert first[2] == second[2]


def test_get_root_ignores_query_string(home, port):
    make_db(home, rows=3)
    status, _, body = fetch(port, "/?refresh=1")
    assert status == 200
    assert '<strong id="run-count">3</strong>' in body.decode("utf-8")


def test_get_root_without_database_returns_200_with_both_unavailable_lines(
    viewer, home, port
):
    status, headers, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "Hello from the factory" in text
    assert (
        count_unavailable_line(home / "factory.db") + LATEST_UNAVAILABLE_LINE in text
    )
    assert 'id="run-count"' not in text
    assert text == viewer.render_page(
        viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE)
    )


def test_get_root_without_database_does_not_create_it(home, port):
    status, _, body = fetch(port)
    assert status == 200
    assert LATEST_UNAVAILABLE_LINE in body.decode("utf-8")
    assert not (home / "factory.db").exists()
    assert list(home.iterdir()) == []


def test_get_root_without_runs_table_returns_200_with_both_unavailable_lines(
    home, port
):
    make_db(home, with_runs_table=False)
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert "Hello from the factory" in text
    assert 'id="run-count-unavailable"' in text
    assert LATEST_UNAVAILABLE_LINE in text
    assert 'id="run-count"' not in text


def test_get_root_with_non_sqlite_file_returns_200_with_both_unavailable_lines(
    home, port
):
    _write_non_sqlite_file(home)
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert 'id="run-count-unavailable"' in text
    assert LATEST_UNAVAILABLE_LINE in text


def test_get_root_with_empty_runs_table_shows_zero_and_no_latest_run(
    viewer, home, port
):
    make_outcome_db(home, [])
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert '<strong id="run-count">0</strong>' in text
    assert "latest-run" not in text
    assert text == viewer.render_page(viewer.PageData(0, None))


def test_get_root_with_runs_lacking_outcome_columns_shows_count_and_unavailable_line(
    viewer, home, port
):
    make_db(home, rows=3)
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert COUNT_LINE.format(3) + LATEST_UNAVAILABLE_LINE in text
    assert "run-count-unavailable" not in text
    assert "latest-run-outcome" not in text
    assert "latest-run-not-ended" not in text
    assert text == viewer.render_page(viewer.PageData(3, viewer.UNREADABLE))


def test_get_root_content_length_matches_body_with_latest_run_unavailable(home, port):
    make_db(home, rows=3)
    _, headers, body = fetch(port)
    assert LATEST_UNAVAILABLE_LINE.encode("utf-8") in body
    assert int(headers["Content-Length"]) == len(body)


def test_get_root_recovers_once_database_appears(home, port):
    _, _, before = fetch(port)
    text = before.decode("utf-8")
    assert 'id="run-count-unavailable"' in text
    assert LATEST_UNAVAILABLE_LINE in text

    make_outcome_db(home, [("passed", "all green"), ("failed", "tests red")])

    _, _, after = fetch(port)
    text = after.decode("utf-8")
    assert '<strong id="run-count">2</strong>' in text
    assert OUTCOME_LINE.format("failed") in text
    assert "unavailable" not in text


def test_get_root_stops_saying_latest_run_unavailable_once_the_columns_are_added(
    home, port
):
    path = make_db(home, rows=2)
    _, _, before = fetch(port)
    assert LATEST_UNAVAILABLE_LINE in before.decode("utf-8")

    execute_sql(path, "ALTER TABLE runs ADD COLUMN outcome TEXT")
    execute_sql(path, "ALTER TABLE runs ADD COLUMN reason TEXT")

    _, _, after = fetch(port)
    text = after.decode("utf-8")
    assert "latest-run-unavailable" not in text
    assert NOT_ENDED_LINE in text
    assert '<strong id="run-count">2</strong>' in text


def test_get_root_leaves_database_file_unchanged(home, port):
    path = make_db(home, rows=3)
    before = path.read_bytes()
    _, _, body = fetch(port)
    assert LATEST_UNAVAILABLE_LINE.encode("utf-8") in body
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_get_unknown_path_returns_404(home, port):
    make_db(home, rows=3)
    status, _, body = fetch(port, "/nope")
    assert status == 404
    assert "Hello from the factory" not in body.decode("utf-8", errors="replace")


def test_get_nested_path_under_root_returns_404(home, port):
    make_db(home, rows=3)
    status, _, _ = fetch(port, "/index.html")
    assert status == 404


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


# ---------------------------------------------------------- HTTP server, latest run


def test_get_root_shows_outcome_and_reason_of_the_ended_latest_run(home, port):
    make_outcome_db(home, [("failed", "old"), ("passed", "all green")])
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert OUTCOME_LINE.format("passed") in text
    assert REASON_LINE.format("all green") in text
    assert '<strong id="run-count">2</strong>' in text
    assert "old" not in text


def test_get_root_shows_not_ended_line_for_a_not_ended_latest_run(home, port):
    make_outcome_db(home, [("passed", "all green"), (None, None)])
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert NOT_ENDED_LINE in text
    assert "latest-run-outcome" not in text
    assert "latest-run-reason" not in text
    assert "all green" not in text


def test_get_root_shows_not_ended_line_for_latest_run_with_empty_strings(home, port):
    make_outcome_db(home, [("", "")])
    _, _, body = fetch(port)
    text = body.decode("utf-8")
    assert NOT_ENDED_LINE in text
    assert "latest-run-outcome" not in text


def test_get_root_omits_reason_when_ended_latest_run_has_no_reason(home, port):
    make_outcome_db(home, [("passed", None)])
    _, _, body = fetch(port)
    text = body.decode("utf-8")
    assert OUTCOME_LINE.format("passed") in text
    assert "latest-run-reason" not in text


def test_get_root_shows_outcome_once_the_latest_run_has_ended(home, port):
    path = make_outcome_db(home, [(None, None)])
    _, _, first = fetch(port)
    assert NOT_ENDED_LINE in first.decode("utf-8")

    execute_sql(path, "UPDATE runs SET outcome = 'failed', reason = 'tests red'")

    status, _, second = fetch(port)
    text = second.decode("utf-8")
    assert status == 200
    assert OUTCOME_LINE.format("failed") in text
    assert REASON_LINE.format("tests red") in text
    assert "latest-run-not-ended" not in text


def test_get_root_shows_a_newer_run_inserted_while_serving(home, port):
    path = make_outcome_db(home, [("passed", "all green")])
    _, _, first = fetch(port)
    assert OUTCOME_LINE.format("passed") in first.decode("utf-8")

    insert_outcome_run(path, None, None)

    _, _, second = fetch(port)
    text = second.decode("utf-8")
    assert NOT_ENDED_LINE in text
    assert "latest-run-outcome" not in text
    assert '<strong id="run-count">2</strong>' in text


def test_get_root_escapes_html_in_outcome_and_reason(home, port):
    make_outcome_db(home, [("<b>&", "<i>why</i>")])
    _, _, body = fetch(port)
    text = body.decode("utf-8")
    assert OUTCOME_LINE.format("&lt;b&gt;&amp;") in text
    assert REASON_LINE.format("&lt;i&gt;why&lt;/i&gt;") in text
    assert "<b>" not in text
    assert "<i>" not in text


def test_get_root_content_length_matches_body_with_non_ascii_reason(home, port):
    make_outcome_db(home, [("passed", "café ✓")])
    _, headers, body = fetch(port)
    assert REASON_LINE.format("café ✓") in body.decode("utf-8")
    assert int(headers["Content-Length"]) == len(body)


def test_get_root_twice_returns_the_same_page_with_a_latest_run(home, port):
    make_outcome_db(home, [("passed", "all green")])
    first = fetch(port)
    second = fetch(port)
    assert first[0] == second[0] == 200
    assert first[2] == second[2]
    assert b'id="latest-run-outcome"' in first[2]


def test_get_root_twice_returns_the_same_page_when_latest_run_is_unavailable(
    home, port
):
    make_db(home, rows=3)
    first = fetch(port)
    second = fetch(port)
    assert first[0] == second[0] == 200
    assert first[2] == second[2]
    assert b'id="latest-run-unavailable"' in first[2]


def test_get_root_leaves_database_with_outcome_columns_unchanged(home, port):
    path = make_outcome_db(home, [("passed", "all green"), (None, None)])
    before = path.read_bytes()
    _, _, body = fetch(port)
    assert b'id="latest-run-not-ended"' in body
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_get_unknown_path_does_not_show_the_latest_run(viewer, home, port):
    make_outcome_db(home, [("passed", "all green")])
    assert viewer.read_page_data().latest == viewer.LatestRun("passed", "all green")
    status, _, body = fetch(port, "/latest")
    assert status == 404
    assert b"latest-run" not in body


def test_get_unknown_path_does_not_open_the_database(viewer, home, port, monkeypatch):
    make_outcome_db(home, [("passed", "all green")])
    assert viewer.read_page_data().count == 1
    calls = record_connects(viewer, monkeypatch)
    status, _, _ = fetch(port, "/nope")
    assert status == 404
    assert calls == []


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
