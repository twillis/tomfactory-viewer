"""Tests for the hello-world observability page (``viewer.py`` at the repository root)."""

import ast
import html
import importlib
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

# Keyword arguments for ``insert_full_run`` that make the run one that has not ended.
RUNNING = {"state": "working", "outcome": None, "ended_at": None}

TEN_WORDS = "one two three four five six seven eight nine ten"

RUNS_LINK_LINE = '<p><a id="runs-link" href="/runs">All runs</a></p>\n'
NO_RUNS_LINE = '<p id="no-runs">No runs recorded yet</p>\n'
RUNS_UNAVAILABLE_LINE = (
    '<p id="runs-unavailable">Runs unavailable: could not read the runs table</p>\n'
)
RUNS_HEAD = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    "<title>Runs - tomfactory viewer</title>\n"
    "</head>\n"
    "<body>\n"
    "<h1>Runs</h1>\n"
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
        f'<td class="run-id">{run_id}</td>'
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


def insert_full_run(
    path,
    run_id=None,
    request="Fix the bug",
    state="ended",
    outcome="accepted",
    created_at=CREATED,
    ended_at=ENDED,
):
    """Insert one row into a ``runs`` table made from ``RUNS_DDL``; ended by default."""
    execute_sql(
        path,
        "INSERT INTO runs "
        "(id, request, target, base_commit, state, outcome, created_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, request, "/tmp/target", "abc123", state, outcome, created_at, ended_at),
    )


def make_runs_db(home, runs=()):
    """Create ``home/factory.db`` with the documented ``runs`` table.

    ``runs`` is a sequence of keyword-argument mappings for ``insert_full_run``,
    inserted in order.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "factory.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(RUNS_DDL)
        conn.commit()
    finally:
        conn.close()
    for run in runs:
        insert_full_run(path, **run)
    return path


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


def _make_runs_table_without_ended_at(home):
    path = make_db(home, with_runs_table=False)
    execute_sql(
        path,
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, state TEXT, outcome TEXT, "
        "request TEXT, created_at TEXT)",
    )
    execute_sql(
        path,
        "INSERT INTO runs (state, outcome, request, created_at) "
        "VALUES ('ended', 'accepted', 'Fix the bug', ?)",
        (CREATED,),
    )


def _make_runs_table_without_request(home):
    path = make_db(home, with_runs_table=False)
    execute_sql(
        path,
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, state TEXT, outcome TEXT, "
        "created_at TEXT, ended_at TEXT)",
    )
    execute_sql(
        path,
        "INSERT INTO runs (state, outcome, created_at, ended_at) "
        "VALUES ('ended', 'accepted', ?, ?)",
        (CREATED, ENDED),
    )


def _make_runs_table_with_only_an_id(home):
    make_db(home, rows=3)


def _make_runs_table_with_only_outcome_and_reason(home):
    make_outcome_db(home, [("accepted", "all green")])


UNREADABLE_RUNS_DATABASES = UNREADABLE_DATABASES + [
    pytest.param(_make_runs_table_without_ended_at, id="runs_has_no_ended_at_column"),
    pytest.param(_make_runs_table_without_request, id="runs_has_no_request_column"),
    pytest.param(_make_runs_table_with_only_an_id, id="runs_has_only_an_id_column"),
    pytest.param(
        _make_runs_table_with_only_outcome_and_reason,
        id="runs_has_only_outcome_and_reason_columns",
    ),
]


class BrokenCursor:
    """A cursor whose every way of reading rows fails."""

    def _fail(self, *args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    __iter__ = fetchone = fetchall = fetchmany = _fail


class BrokenConnection:
    """A connection whose statement fails, or whose rows cannot be fetched."""

    def __init__(self, real, fail_on):
        self._real = real
        self._fail_on = fail_on
        self.close_calls = 0

    def execute(self, sql, *args, **kwargs):
        if self._fail_on == "execute":
            raise sqlite3.OperationalError("database is locked")
        return BrokenCursor()

    def close(self):
        self.close_calls += 1
        self._real.close()


def break_connections(viewer, monkeypatch, fail_on):
    """Make ``viewer.sqlite3.connect`` return broken connections; return those made."""
    made = []
    real_connect = sqlite3.connect

    def breaking_connect(*args, **kwargs):
        broken = BrokenConnection(real_connect(*args, **kwargs), fail_on)
        made.append(broken)
        return broken

    monkeypatch.setattr(viewer.sqlite3, "connect", breaking_connect)
    return made


# ---------------------------------------------------- RunSummary and RunsPageData


def test_run_summary_has_its_six_fields_in_order(viewer):
    assert viewer.RunSummary._fields == (
        "id",
        "state",
        "outcome",
        "request",
        "created_at",
        "ended_at",
    )


def test_run_summary_keeps_the_values_it_is_given(viewer):
    run = viewer.RunSummary(7, "ended", "accepted", "Fix the bug", CREATED_DT, ENDED_DT)
    assert run.id == 7
    assert run.state == "ended"
    assert run.outcome == "accepted"
    assert run.request == "Fix the bug"
    assert run.created_at == CREATED_DT
    assert run.ended_at == ENDED_DT


def test_runs_page_data_has_runs_and_read_at_fields_in_that_order(viewer):
    data = viewer.RunsPageData((), CREATED_DT)
    assert viewer.RunsPageData._fields == ("runs", "read_at")
    assert data.runs == ()
    assert data.read_at == CREATED_DT


def test_runs_page_data_requires_read_at_to_be_given(viewer):
    runs_page_data = viewer.RunsPageData
    with pytest.raises(TypeError):
        runs_page_data(())


# ------------------------------------------------------ RunSummary: ended and status


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "needs-me"])
def test_run_summary_with_an_outcome_has_ended_and_its_status_is_the_outcome(
    viewer, outcome
):
    run = ended_summary(viewer, outcome=outcome)
    assert run.ended is True
    assert run.status == outcome


@pytest.mark.parametrize("state", ["planning", "awaiting_approval", "working"])
def test_run_summary_without_an_outcome_has_not_ended_and_its_status_is_the_state(
    viewer, state
):
    run = running_summary(viewer, state=state)
    assert run.ended is False
    assert run.status == state


def test_run_summary_ended_is_decided_by_the_outcome_not_the_state(viewer):
    stale_state = ended_summary(viewer, state="working", outcome="rejected")
    assert stale_state.ended is True
    assert stale_state.status == "rejected"

    no_outcome = viewer.RunSummary(7, "ended", "", "Fix the bug", CREATED_DT, ENDED_DT)
    assert no_outcome.ended is False
    assert no_outcome.status == "ended"


# ------------------------------------------------------------ RunSummary: duration


def test_run_summary_duration_of_an_ended_run_is_from_created_to_ended(viewer):
    run = ended_summary(viewer)
    assert run.duration(ENDED_DT) == timedelta(seconds=187)


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


def test_run_summary_duration_of_a_running_run_grows_with_now(viewer):
    run = running_summary(viewer)
    earlier = run.duration(CREATED_DT + timedelta(seconds=10))
    later = run.duration(CREATED_DT + timedelta(seconds=11))
    assert later - earlier == timedelta(seconds=1)


def test_run_summary_duration_is_none_for_an_ended_run_without_created_at(viewer):
    run = ended_summary(viewer, created_at=None)
    assert run.duration(ENDED_DT) is None


def test_run_summary_duration_is_none_for_a_running_run_without_created_at(viewer):
    run = running_summary(viewer, created_at=None)
    assert run.duration(ENDED_DT) is None


def test_run_summary_duration_is_none_for_an_ended_run_without_ended_at(viewer):
    run = ended_summary(viewer, ended_at=None)
    assert run.duration(ENDED_DT + timedelta(days=1)) is None


def test_run_summary_duration_is_none_when_both_times_are_missing(viewer):
    run = ended_summary(viewer, created_at=None, ended_at=None)
    assert run.duration(ENDED_DT) is None


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


def test_run_summary_duration_is_zero_when_a_run_ended_the_moment_it_was_created(
    viewer,
):
    run = ended_summary(viewer, ended_at=CREATED_DT)
    assert run.duration(ENDED_DT) == timedelta(0)


# ------------------------------------------- read_runs_page_data: readable states


def test_read_runs_page_data_returns_a_runs_page_data(viewer, home):
    make_runs_db(home, [{}])
    assert isinstance(viewer.read_runs_page_data(), viewer.RunsPageData)


def test_read_runs_page_data_runs_is_a_tuple_of_run_summaries(viewer, home):
    make_runs_db(home, [{}, RUNNING])
    runs = viewer.read_runs_page_data().runs
    assert type(runs) is tuple
    assert len(runs) == 2
    assert all(isinstance(run, viewer.RunSummary) for run in runs)


def test_read_runs_page_data_maps_every_field_of_an_ended_run(viewer, home):
    make_runs_db(
        home,
        [{"run_id": 7, "request": "Add a page at /runs", "outcome": "rejected"}],
    )
    assert viewer.read_runs_page_data().runs == (
        viewer.RunSummary(
            7, "ended", "rejected", "Add a page at /runs", CREATED_DT, ENDED_DT
        ),
    )


def test_read_runs_page_data_gives_a_running_run_empty_outcome_and_no_ended_at(
    viewer, home
):
    make_runs_db(home, [{"run_id": 4, "request": "Still going", **RUNNING}])
    (run,) = viewer.read_runs_page_data().runs
    assert run == viewer.RunSummary(4, "working", "", "Still going", CREATED_DT, None)
    assert run.outcome == ""
    assert run.ended_at is None


@pytest.mark.parametrize("state", ["planning", "awaiting_approval", "working"])
def test_read_runs_page_data_returns_the_stored_state_of_a_run_that_has_not_ended(
    viewer, home, state
):
    make_runs_db(home, [{"state": state, "outcome": None, "ended_at": None}])
    (run,) = viewer.read_runs_page_data().runs
    assert run.state == state
    assert run.outcome == ""


def test_read_runs_page_data_id_is_an_int(viewer, home):
    make_runs_db(home, [{"run_id": 12}])
    (run,) = viewer.read_runs_page_data().runs
    assert type(run.id) is int
    assert run.id == 12


def test_read_runs_page_data_returns_runs_highest_id_first(viewer, home):
    make_runs_db(home, [{"request": "first"}, {"request": "second"}, RUNNING])
    runs = viewer.read_runs_page_data().runs
    assert [run.id for run in runs] == [3, 2, 1]
    assert [run.request for run in runs][1:] == ["second", "first"]


def test_read_runs_page_data_orders_by_id_not_by_insertion_order(viewer, home):
    make_runs_db(home, [{"run_id": 50}, {"run_id": 7}, {"run_id": 200}, {"run_id": 9}])
    runs = viewer.read_runs_page_data().runs
    assert [run.id for run in runs] == [200, 50, 9, 7]


def test_read_runs_page_data_orders_by_id_not_by_created_at(viewer, home):
    make_runs_db(
        home,
        [
            {"run_id": 1, "created_at": "2030-01-01T00:00:00Z", "ended_at": None},
            {"run_id": 2, "created_at": "2020-01-01T00:00:00Z", "ended_at": None},
        ],
    )
    assert [run.id for run in viewer.read_runs_page_data().runs] == [2, 1]


def test_read_runs_page_data_returns_a_single_run(viewer, home):
    make_runs_db(home, [{}])
    assert len(viewer.read_runs_page_data().runs) == 1


def test_read_runs_page_data_returns_every_run_when_there_are_many(viewer, home):
    path = make_runs_db(home)
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO runs "
            "(request, target, base_commit, state, outcome, created_at, ended_at) "
            "VALUES (?, '/tmp/target', 'abc123', 'ended', 'accepted', ?, ?)",
            [(f"request number {n}", CREATED, ENDED) for n in range(1, 301)],
        )
        conn.commit()
    finally:
        conn.close()

    runs = viewer.read_runs_page_data().runs
    assert len(runs) == 300
    assert [run.id for run in runs] == list(range(300, 0, -1))
    assert runs[0].request == "request number 300"
    assert runs[-1].request == "request number 1"


def test_read_runs_page_data_returns_the_request_in_full_and_unchanged(viewer, home):
    request = "  <b>café</b> & 'quotes' ✓\n\nsecond paragraph " + "word " * 200
    make_runs_db(home, [{"request": request}])
    (run,) = viewer.read_runs_page_data().runs
    assert run.request == request


def test_read_runs_page_data_works_when_home_path_contains_a_space(
    viewer, tmp_path, monkeypatch
):
    spaced = tmp_path / "factory home"
    make_runs_db(spaced, [{}, {}])
    monkeypatch.setenv("TOMFACTORY_HOME", str(spaced))
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


def test_read_runs_page_data_parses_timestamps_as_utc_aware_datetimes(viewer, home):
    make_runs_db(home, [{}])
    (run,) = viewer.read_runs_page_data().runs
    assert isinstance(run.created_at, datetime)
    assert isinstance(run.ended_at, datetime)
    assert run.created_at.utcoffset() == timedelta(0)
    assert run.ended_at.utcoffset() == timedelta(0)
    assert run.created_at == CREATED_DT
    assert run.ended_at == ENDED_DT


MALFORMED_TIMES = [
    pytest.param("yesterday", id="words"),
    pytest.param("", id="empty_string"),
    pytest.param("2026-11-18 21:04:05", id="space_and_no_z"),
    pytest.param("2026-11-18T21:04:05", id="no_z"),
    pytest.param("2026-11-18T21:04:05+00:00", id="numeric_offset"),
    pytest.param("2026-11-18T21:04:05.250Z", id="fractional_seconds"),
    pytest.param("2026-13-45T99:99:99Z", id="impossible_date"),
    pytest.param("2026-11-18", id="date_only"),
    pytest.param("1763500000", id="epoch_seconds"),
    pytest.param(b"\x00\x01binary", id="blob"),
]


@pytest.mark.parametrize("stored", MALFORMED_TIMES)
def test_read_runs_page_data_created_at_is_none_when_stored_as(viewer, home, stored):
    make_runs_db(home, [{"request": "Odd time", "created_at": stored}])
    data = viewer.read_runs_page_data()
    assert data.runs == (
        viewer.RunSummary(1, "ended", "accepted", "Odd time", None, ENDED_DT),
    )


@pytest.mark.parametrize("stored", MALFORMED_TIMES)
def test_read_runs_page_data_ended_at_is_none_when_stored_as(viewer, home, stored):
    make_runs_db(home, [{"request": "Odd time", "ended_at": stored}])
    data = viewer.read_runs_page_data()
    assert data.runs == (
        viewer.RunSummary(1, "ended", "accepted", "Odd time", CREATED_DT, None),
    )


def test_read_runs_page_data_keeps_other_runs_when_one_has_a_malformed_time(
    viewer, home
):
    make_runs_db(home, [{}, {"created_at": "garbage"}, {}])
    runs = viewer.read_runs_page_data().runs
    assert [run.id for run in runs] == [3, 2, 1]
    assert [run.created_at for run in runs] == [CREATED_DT, None, CREATED_DT]


# ------------------------------------------------ read_runs_page_data: empty table


def test_read_runs_page_data_gives_an_empty_tuple_for_an_empty_runs_table(viewer, home):
    make_runs_db(home)
    runs = viewer.read_runs_page_data().runs
    assert runs == ()
    assert type(runs) is tuple
    assert runs is not viewer.UNREADABLE


# ------------------------------------------------- read_runs_page_data: unreadable


@pytest.mark.parametrize("break_database", UNREADABLE_RUNS_DATABASES)
def test_read_runs_page_data_runs_is_unreadable_when(viewer, home, break_database):
    break_database(home)
    data = viewer.read_runs_page_data()
    assert isinstance(data, viewer.RunsPageData)
    assert data.runs is viewer.UNREADABLE
    assert data.runs != ()


def test_read_runs_page_data_runs_is_unreadable_when_home_directory_is_missing(
    viewer, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOMFACTORY_HOME", str(tmp_path / "does-not-exist"))
    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE


def test_read_runs_page_data_runs_is_unreadable_when_connect_raises(
    viewer, home, monkeypatch
):
    make_runs_db(home, [{}])

    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(viewer.sqlite3, "connect", failing_connect)
    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE


def test_read_runs_page_data_runs_is_unreadable_when_the_statement_fails(
    viewer, home, monkeypatch
):
    make_runs_db(home, [{}])
    made = break_connections(viewer, monkeypatch, fail_on="execute")

    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert len(made) == 1
    assert made[0].close_calls == 1


def test_read_runs_page_data_runs_is_unreadable_when_fetching_rows_fails(
    viewer, home, monkeypatch
):
    make_runs_db(home, [{}])
    made = break_connections(viewer, monkeypatch, fail_on="fetch")

    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert len(made) == 1
    assert made[0].close_calls == 1


# ----------------------------------------------------- read_runs_page_data: read_at


def test_read_runs_page_data_read_at_is_the_utc_time_of_the_call(viewer, home):
    make_runs_db(home, [{}])
    before = datetime.now(timezone.utc)
    data = viewer.read_runs_page_data()
    after = datetime.now(timezone.utc)
    assert data.read_at.utcoffset() == timedelta(0)
    assert before <= data.read_at <= after


def test_read_runs_page_data_read_at_is_set_when_the_runs_are_unreadable(viewer, home):
    before = datetime.now(timezone.utc)
    data = viewer.read_runs_page_data()
    after = datetime.now(timezone.utc)
    assert data.runs is viewer.UNREADABLE
    assert data.read_at.utcoffset() == timedelta(0)
    assert before <= data.read_at <= after


def test_read_runs_page_data_read_at_is_set_for_an_empty_runs_table(viewer, home):
    make_runs_db(home)
    before = datetime.now(timezone.utc)
    data = viewer.read_runs_page_data()
    after = datetime.now(timezone.utc)
    assert data.runs == ()
    assert before <= data.read_at <= after


def test_read_runs_page_data_read_at_does_not_go_backwards_between_calls(viewer, home):
    make_runs_db(home, [{}])
    first = viewer.read_runs_page_data()
    second = viewer.read_runs_page_data()
    assert first.read_at <= second.read_at


# ---------------------------------- read_runs_page_data: one read-only connection


def test_read_runs_page_data_opens_one_read_only_connection(viewer, home, monkeypatch):
    make_runs_db(home, [{}, {}, RUNNING])
    calls = record_connects(viewer, monkeypatch)

    assert len(viewer.read_runs_page_data().runs) == 3
    assert_one_read_only_connect(calls)


def test_read_runs_page_data_opens_one_read_only_connection_for_an_empty_runs_table(
    viewer, home, monkeypatch
):
    make_runs_db(home)
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_runs_page_data().runs == ()
    assert_one_read_only_connect(calls)


def test_read_runs_page_data_tries_to_connect_only_once_when_the_database_is_missing(
    viewer, home, monkeypatch
):
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert_one_read_only_connect(calls)


def test_read_runs_page_data_opens_one_connection_per_call(viewer, home, monkeypatch):
    make_runs_db(home, [{}])
    calls = record_connects(viewer, monkeypatch)

    viewer.read_runs_page_data()
    viewer.read_runs_page_data()
    assert len(calls) == 2


def test_read_runs_page_data_closes_its_connection_once_after_a_successful_read(
    viewer, home, monkeypatch
):
    make_runs_db(home, [{}, {}])
    proxies = wrap_connections(viewer, monkeypatch)

    assert len(viewer.read_runs_page_data().runs) == 2
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


def test_read_runs_page_data_closes_its_connection_when_the_table_is_missing(
    viewer, home, monkeypatch
):
    make_db(home, with_runs_table=False)
    proxies = wrap_connections(viewer, monkeypatch)

    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


def test_read_runs_page_data_closes_its_connection_when_a_column_is_missing(
    viewer, home, monkeypatch
):
    _make_runs_table_without_ended_at(home)
    proxies = wrap_connections(viewer, monkeypatch)

    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


# ----------------------------------- read_runs_page_data: never writes or creates


def test_read_runs_page_data_does_not_create_a_missing_database_file(viewer, home):
    viewer.read_runs_page_data()
    viewer.read_runs_page_data()
    assert not (home / "factory.db").exists()
    assert list(home.iterdir()) == []


def test_read_runs_page_data_does_not_create_a_missing_home_directory(
    viewer, tmp_path, monkeypatch
):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("TOMFACTORY_HOME", str(missing))
    viewer.read_runs_page_data()
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_read_runs_page_data_leaves_the_database_file_unchanged(viewer, home):
    path = make_runs_db(home, [{}, {}, RUNNING])
    before = path.read_bytes()
    assert len(viewer.read_runs_page_data().runs) == 3
    assert len(viewer.read_runs_page_data().runs) == 3
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_runs_page_data_leaves_an_empty_runs_table_unchanged(viewer, home):
    path = make_runs_db(home)
    before = path.read_bytes()
    assert viewer.read_runs_page_data().runs == ()
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_runs_page_data_leaves_a_non_sqlite_file_unchanged(viewer, home):
    _write_non_sqlite_file(home)
    path = home / "factory.db"
    before = path.read_bytes()
    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_runs_page_data_leaves_an_empty_database_file_empty(viewer, home):
    _write_empty_file(home)
    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    assert (home / "factory.db").read_bytes() == b""
    assert [p.name for p in home.iterdir()] == ["factory.db"]


# ------------------------------------------------ read_runs_page_data: between calls


def test_read_runs_page_data_called_twice_returns_the_same_runs(viewer, home):
    make_runs_db(home, [{}, RUNNING])
    first = viewer.read_runs_page_data()
    second = viewer.read_runs_page_data()
    assert first.runs == second.runs
    assert len(first.runs) == 2


def test_read_runs_page_data_sees_a_newly_inserted_run_on_the_next_call(viewer, home):
    path = make_runs_db(home, [{}])
    assert [run.id for run in viewer.read_runs_page_data().runs] == [1]
    insert_full_run(path, request="A newer run", **RUNNING)
    runs = viewer.read_runs_page_data().runs
    assert [run.id for run in runs] == [2, 1]
    assert runs[0].request == "A newer run"


def test_read_runs_page_data_sees_a_run_ending_on_the_next_call(viewer, home):
    path = make_runs_db(home, [RUNNING])
    (before,) = viewer.read_runs_page_data().runs
    assert before.outcome == ""
    assert before.ended_at is None

    execute_sql(
        path,
        "UPDATE runs SET state = 'ended', outcome = 'needs-me', ended_at = ?",
        (ENDED,),
    )

    (after,) = viewer.read_runs_page_data().runs
    assert after == viewer.RunSummary(
        1, "ended", "needs-me", "Fix the bug", CREATED_DT, ENDED_DT
    )


def test_read_runs_page_data_recovers_once_the_database_appears(viewer, home):
    assert viewer.read_runs_page_data().runs is viewer.UNREADABLE
    make_runs_db(home, [{}])
    assert [run.id for run in viewer.read_runs_page_data().runs] == [1]


def test_read_runs_page_data_reads_the_environment_on_every_call(
    viewer, tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    make_runs_db(first, [{"request": "from first"}])
    make_runs_db(second, [{}, {"request": "from second"}])
    monkeypatch.setenv("TOMFACTORY_HOME", str(first))
    assert [run.request for run in viewer.read_runs_page_data().runs] == ["from first"]
    monkeypatch.setenv("TOMFACTORY_HOME", str(second))
    assert viewer.read_runs_page_data().runs[0].request == "from second"


def test_read_page_data_still_reads_the_documented_runs_table(viewer, home):
    make_runs_db(home, [{}, RUNNING])
    assert viewer.read_page_data() == viewer.PageData(2, viewer.LatestRun("", ""))
    assert len(viewer.read_runs_page_data().runs) == 2


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


def test_render_runs_page_has_its_own_title_and_heading(viewer):
    page = render_runs(viewer, [ended_summary(viewer)])
    assert page.startswith("<!doctype html>\n")
    assert "<title>Runs - tomfactory viewer</title>" in page
    assert "<h1>Runs</h1>\n" in page
    assert "Hello from the factory" not in page
    assert page.endswith(DOCUMENT_TAIL)


def test_render_runs_page_takes_one_runs_page_data_and_no_other_argument(viewer):
    data = viewer.RunsPageData((), ENDED_DT)
    assert NO_RUNS_LINE in viewer.render_runs_page(data)
    with pytest.raises(TypeError):
        viewer.render_runs_page(data, ENDED_DT)
    with pytest.raises(TypeError):
        viewer.render_runs_page(data, now=ENDED_DT)


def test_render_runs_page_called_twice_with_the_same_data_returns_the_same_page(viewer):
    data = viewer.RunsPageData(
        (running_summary(viewer, run_id=2), ended_summary(viewer, run_id=1)), ENDED_DT
    )
    assert viewer.render_runs_page(data) == viewer.render_runs_page(data)
    assert 'id="run-2"' in viewer.render_runs_page(data)


# ------------------------------------------------------- render_runs_page: the rows


def test_render_runs_page_shows_the_exact_row_of_an_ended_run(viewer):
    run = ended_summary(viewer, run_id=6, outcome="rejected", request="Tidy the docs")
    page = render_runs(viewer, [run], read_at=ENDED_DT + timedelta(days=3))
    assert run_row(6, "rejected", "Tidy the docs", CREATED_TEXT, "3m 7s") in page


def test_render_runs_page_shows_the_exact_row_of_a_running_run_with_so_far(viewer):
    run = running_summary(viewer, run_id=7, request="Add a page at /runs")
    page = render_runs(viewer, [run], read_at=CREATED_DT + timedelta(seconds=187))
    assert (
        run_row(7, "working", "Add a page at /runs", CREATED_TEXT, "3m 7s so far")
        in page
    )


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "needs-me"])
def test_render_runs_page_shows_the_outcome_as_the_status_of_an_ended_run(
    viewer, outcome
):
    page = render_runs(viewer, [ended_summary(viewer, outcome=outcome)])
    assert f'<td class="run-status">{outcome}</td>' in page
    assert '<td class="run-status">ended</td>' not in page


@pytest.mark.parametrize("state", ["planning", "awaiting_approval", "working"])
def test_render_runs_page_shows_the_state_as_the_status_of_a_run_that_has_not_ended(
    viewer, state
):
    page = render_runs(viewer, [running_summary(viewer, state=state)])
    assert f'<td class="run-status">{state}</td>' in page


def test_render_runs_page_ended_row_does_not_depend_on_read_at(viewer):
    runs = [ended_summary(viewer)]
    early = render_runs(viewer, runs, read_at=ENDED_DT)
    late = render_runs(viewer, runs, read_at=ENDED_DT + timedelta(days=30))
    assert early == late
    assert "so far" not in early


def test_render_runs_page_running_duration_is_measured_to_read_at(viewer):
    runs = [running_summary(viewer)]
    early = render_runs(viewer, runs, read_at=CREATED_DT + timedelta(seconds=5))
    late = render_runs(viewer, runs, read_at=CREATED_DT + timedelta(hours=2, seconds=7))
    assert duration_cell("5s so far") in early
    assert duration_cell("2h 0m 7s so far") in late


def test_render_runs_page_keeps_rows_in_the_order_given(viewer):
    runs = [
        ended_summary(viewer, run_id=3),
        ended_summary(viewer, run_id=9),
        ended_summary(viewer, run_id=1),
    ]
    page = render_runs(viewer, runs)
    assert re.findall(r'<tr id="run-(\d+)">', page) == ["3", "9", "1"]


def test_render_runs_page_shows_one_row_per_run_inside_one_table(viewer):
    runs = [running_summary(viewer, run_id=3)] + [
        ended_summary(viewer, run_id=n) for n in (2, 1)
    ]
    page = render_runs(viewer, runs)
    assert page.count("<table") == 1
    assert page.count('<tr id="run-') == 3
    assert page.count("so far") == 1
    assert RUNS_TABLE_OPEN + run_row(3, "working", "Fix the bug", duration="3m 7s so far") in page
    assert page.endswith(
        run_row(1, "accepted", "Fix the bug") + RUNS_TABLE_CLOSE + DOCUMENT_TAIL
    )


def test_render_runs_page_shows_every_run_when_there_are_many(viewer):
    runs = [ended_summary(viewer, run_id=n) for n in range(300, 0, -1)]
    page = render_runs(viewer, runs)
    assert page.count('<tr id="run-') == 300
    assert 'id="run-300"' in page
    assert 'id="run-1"' in page


def test_render_runs_page_shows_a_table_and_no_message_when_there_are_runs(viewer):
    page = render_runs(viewer, [ended_summary(viewer)])
    assert '<table id="runs">' in page
    assert "no-runs" not in page
    assert "runs-unavailable" not in page


# -------------------------------------------- render_runs_page: no runs, unreadable


def test_render_runs_page_says_no_runs_recorded_yet_without_a_table(viewer):
    page = render_runs(viewer, [])
    assert NO_RUNS_LINE in page
    assert "<table" not in page
    assert "<tr" not in page
    assert "runs-unavailable" not in page


def test_render_runs_page_says_runs_unavailable_without_a_table(viewer):
    page = viewer.render_runs_page(viewer.RunsPageData(viewer.UNREADABLE, ENDED_DT))
    assert RUNS_UNAVAILABLE_LINE in page
    assert "<table" not in page
    assert "<tr" not in page
    assert "no-runs" not in page


def test_render_runs_page_differs_between_no_runs_and_unreadable_runs(viewer):
    empty = render_runs(viewer, [])
    unreadable = viewer.render_runs_page(
        viewer.RunsPageData(viewer.UNREADABLE, ENDED_DT)
    )
    assert empty != unreadable
    assert "No runs recorded yet" not in unreadable


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


def test_render_runs_page_cuts_a_very_long_request_to_ten_words(viewer):
    page = render_request(viewer, TEN_WORDS + " tail" * 5000)
    assert request_cell(TEN_WORDS + "…") in page
    assert "tail" not in page


def test_render_runs_page_shows_a_nine_word_request_in_full(viewer):
    nine = "one two three four five six seven eight nine"
    page = render_request(viewer, nine)
    assert request_cell(nine) in page
    assert "…" not in page


def test_render_runs_page_shows_a_one_word_request_in_full(viewer):
    page = render_request(viewer, "Refactor")
    assert request_cell("Refactor") in page
    assert "…" not in page


def test_render_runs_page_shows_a_long_single_word_in_full(viewer):
    word = "x" * 500
    page = render_request(viewer, word)
    assert request_cell(word) in page
    assert "…" not in page


def test_render_runs_page_shows_an_empty_cell_for_an_empty_request(viewer):
    page = render_request(viewer, "")
    assert request_cell("") in page
    assert "…" not in page


def test_render_runs_page_shows_an_empty_cell_for_a_whitespace_only_request(viewer):
    page = render_request(viewer, " \n\t  \n")
    assert request_cell("") in page
    assert "…" not in page


def test_render_runs_page_collapses_newlines_and_tabs_in_the_request_to_spaces(viewer):
    page = render_request(viewer, "Add a page\n\nat /runs\tlisting\r\nevery   run")
    assert request_cell("Add a page at /runs listing every run") in page


def test_render_runs_page_strips_whitespace_around_the_request(viewer):
    page = render_request(viewer, "\n  Fix the bug  \n")
    assert request_cell("Fix the bug") in page


def test_render_runs_page_does_not_add_an_ellipsis_for_trailing_whitespace(viewer):
    page = render_request(viewer, TEN_WORDS + "   \n\n")
    assert request_cell(TEN_WORDS) in page
    assert "…" not in page


def test_render_runs_page_counts_words_across_lines(viewer):
    request = "one two three\nfour five six\nseven eight nine\nten eleven twelve"
    page = render_request(viewer, request)
    assert request_cell(TEN_WORDS + "…") in page
    assert "eleven" not in page


def test_render_runs_page_keeps_punctuation_attached_to_the_tenth_word(viewer):
    request = (
        "Add a page at /runs listing every run, newest first: its id, its state"
    )
    page = render_request(viewer, request)
    assert (
        request_cell("Add a page at /runs listing every run, newest first:…") in page
    )


def test_render_runs_page_keeps_non_ascii_text_in_the_request(viewer):
    page = render_request(viewer, "Ajouter un café ✓")
    assert request_cell("Ajouter un café ✓") in page


# ----------------------------------------------------- render_runs_page: escaping


def test_render_runs_page_escapes_markup_in_the_request(viewer):
    page = render_request(viewer, "<script>alert(1)</script> & more")
    assert request_cell("&lt;script&gt;alert(1)&lt;/script&gt; &amp; more") in page
    assert "<script>" not in page


def test_render_runs_page_escapes_quotes_in_the_request(viewer):
    request = "say \"hi\" to 'them'"
    page = render_request(viewer, request)
    assert request_cell(html.escape(request)) in page
    assert 'say "hi"' not in page


def test_render_runs_page_escapes_markup_in_a_shortened_request(viewer):
    page = render_request(viewer, "<b>one</b> two three four five six seven eight nine & eleven")
    assert (
        request_cell(
            "&lt;b&gt;one&lt;/b&gt; two three four five six seven eight nine &amp;…"
        )
        in page
    )
    assert "<b>" not in page


def test_render_runs_page_escapes_markup_in_the_outcome(viewer):
    page = render_runs(viewer, [ended_summary(viewer, outcome="<b>&")])
    assert '<td class="run-status">&lt;b&gt;&amp;</td>' in page
    assert "<b>" not in page


def test_render_runs_page_escapes_markup_in_the_state(viewer):
    state = '<i>"working"</i>'
    page = render_runs(viewer, [running_summary(viewer, state=state)])
    assert '<td class="run-status">' + html.escape(state) + "</td>" in page
    assert "<i>" not in page


# ------------------------------------------- render_runs_page: created and duration


def test_render_runs_page_shows_created_in_utc(viewer):
    created = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    run = ended_summary(viewer, created_at=created, ended_at=created)
    page = render_runs(viewer, [run])
    assert '<td class="run-created">2025-01-02 03:04:05 UTC</td>' in page


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


@pytest.mark.parametrize("seconds, text", DURATIONS)
def test_render_runs_page_formats_the_duration_so_far_of_a_run_running_for(
    viewer, seconds, text
):
    page = render_runs(
        viewer,
        [running_summary(viewer)],
        read_at=CREATED_DT + timedelta(seconds=seconds),
    )
    assert duration_cell(text + " so far") in page


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


def test_render_runs_page_shows_zero_seconds_for_a_run_that_ended_before_it_began(
    viewer,
):
    run = ended_summary(viewer, ended_at=CREATED_DT - timedelta(minutes=5))
    page = render_runs(viewer, [run])
    assert duration_cell("0s") in page
    assert 'run-duration">-' not in page


def test_render_runs_page_shows_zero_seconds_so_far_when_read_before_the_run_began(
    viewer,
):
    page = render_runs(
        viewer, [running_summary(viewer)], read_at=CREATED_DT - timedelta(minutes=5)
    )
    assert duration_cell("0s so far") in page


# --------------------------------- render_runs_page does not read database or clock


def test_render_runs_page_does_not_open_the_database_or_look_up_its_path(
    viewer, home, monkeypatch
):
    make_runs_db(home, [{"request": "from the database"}])

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("render_runs_page must not open the database")

    def forbidden_db_path():
        raise AssertionError("render_runs_page must not look up the database path")

    monkeypatch.setattr(viewer.sqlite3, "connect", forbidden_connect)
    monkeypatch.setattr(viewer, "db_path", forbidden_db_path)

    page = render_runs(viewer, [ended_summary(viewer, request="given by the caller")])
    assert request_cell("given by the caller") in page
    assert "from the database" not in page

    assert NO_RUNS_LINE in render_runs(viewer, [])
    assert RUNS_UNAVAILABLE_LINE in viewer.render_runs_page(
        viewer.RunsPageData(viewer.UNREADABLE, ENDED_DT)
    )


def test_render_runs_page_with_no_runs_ignores_runs_in_the_database(viewer, home):
    make_runs_db(home, [{"request": "from the database"}])
    page = render_runs(viewer, [])
    assert NO_RUNS_LINE in page
    assert "from the database" not in page


def test_render_runs_page_uses_read_at_and_not_the_current_time(viewer):
    long_ago = datetime(2001, 1, 1, tzinfo=timezone.utc)
    run = running_summary(viewer, created_at=long_ago)
    page = render_runs(viewer, [run], read_at=long_ago + timedelta(seconds=42))
    assert duration_cell("42s so far") in page


# -------------------------------------------------- render_page: the link to /runs


def test_render_page_links_to_all_runs_between_the_heading_and_the_count_line(
    viewer, home
):
    page = viewer.render_page(viewer.PageData(3, None))
    assert (
        "<h1>Hello from the factory</h1>\n" + RUNS_LINK_LINE + COUNT_LINE.format(3)
        in page
    )


def test_render_page_links_to_all_runs_when_the_count_is_unreadable(viewer, home):
    page = viewer.render_page(viewer.PageData(viewer.UNREADABLE, viewer.UNREADABLE))
    assert (
        "<h1>Hello from the factory</h1>\n"
        + RUNS_LINK_LINE
        + count_unavailable_line(home / "factory.db")
        in page
    )


def test_render_page_links_to_all_runs_exactly_once(viewer, home):
    page = viewer.render_page(
        viewer.PageData(3, viewer.LatestRun("accepted", "all green"))
    )
    assert page.count('id="runs-link"') == 1
    assert page.count('href="/runs"') == 1


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


# ------------------------------------------------------------- HTTP server, /runs


def test_get_runs_returns_200_html_in_utf8(home, port):
    make_runs_db(home, [{}])
    status, headers, body = fetch(port, "/runs")
    assert status == 200
    assert headers["Content-Type"].replace(" ", "").lower() == "text/html;charset=utf-8"
    assert "<h1>Runs</h1>" in body.decode("utf-8")


def test_get_runs_content_length_matches_body(home, port):
    make_runs_db(home, [{}])
    status, headers, body = fetch(port, "/runs")
    assert status == 200
    assert headers["Content-Length"] is not None
    assert int(headers["Content-Length"]) == len(body)
    assert len(body) > 0


def test_get_runs_content_length_matches_body_with_non_ascii_text(home, port):
    make_runs_db(home, [{"request": "café ✓ " + TEN_WORDS + " eleven"}])
    status, headers, body = fetch(port, "/runs")
    assert status == 200
    assert "café ✓ one two three four five six seven eight…" in body.decode("utf-8")
    assert int(headers["Content-Length"]) == len(body)
    assert len(body) > len(body.decode("utf-8"))


def test_get_runs_shows_the_row_of_the_one_run(home, port):
    make_runs_db(home, [{"run_id": 7, "request": "Add a page at /runs"}])
    status, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert run_row(7, "accepted", "Add a page at /runs", CREATED_TEXT, "3m 7s") in text
    assert text.count('<tr id="run-') == 1


def test_get_runs_body_is_the_runs_page_rendered_from_the_runs_page_data(
    viewer, home, port
):
    # Every run here has ended, so the page does not depend on when it was read.
    make_runs_db(home, [{"request": "first"}, {"outcome": "rejected"}])
    status, _, body = fetch(port, "/runs")
    assert status == 200
    assert body.decode("utf-8") == viewer.render_runs_page(viewer.read_runs_page_data())
    assert b'<table id="runs">' in body


def test_get_runs_lists_runs_newest_first(home, port):
    make_runs_db(home, [{"run_id": 5}, {"run_id": 12}, {"run_id": 9}])
    _, _, body = fetch(port, "/runs")
    assert re.findall(r'<tr id="run-(\d+)">', body.decode("utf-8")) == ["12", "9", "5"]


def test_get_runs_shows_a_running_run_with_its_state_and_duration_so_far(home, port):
    make_runs_db(
        home,
        [{}, {"request": "Still going", "created_at": "2020-01-01T00:00:00Z", **RUNNING}],
    )
    status, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert re.search(
        r'<tr id="run-2"><td class="run-id">2</td>'
        r'<td class="run-status">working</td>'
        r'<td class="run-request">Still going</td>'
        r'<td class="run-created">2020-01-01 00:00:00 UTC</td>'
        r'<td class="run-duration">\d+d \d+h \d+m \d+s so far</td></tr>\n',
        text,
    )
    assert run_row(1, "accepted", "Fix the bug") in text
    assert text.count("so far") == 1


def test_get_runs_escapes_markup_in_the_request(home, port):
    make_runs_db(home, [{"request": "<script>alert(1)</script> & more"}])
    _, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert request_cell("&lt;script&gt;alert(1)&lt;/script&gt; &amp; more") in text
    assert "<script>" not in text


def test_get_runs_ignores_the_query_string(home, port):
    make_runs_db(home, [{"run_id": 7}])
    status, _, body = fetch(port, "/runs?x=1")
    assert status == 200
    assert 'id="run-7"' in body.decode("utf-8")


@pytest.mark.parametrize(
    "path", ["/runs/", "/runs/7", "/runs.html", "/run", "/Runs", "/runs/?x=1"]
)
def test_get_runs_is_served_only_at_exactly_runs_not_at(home, port, path):
    make_runs_db(home, [{"run_id": 7}])
    assert fetch(port, "/runs")[0] == 200
    status, _, body = fetch(port, path)
    assert status == 404
    assert b'id="run-7"' not in body


def test_get_runs_with_a_trailing_slash_does_not_open_the_database(
    viewer, home, port, monkeypatch
):
    make_runs_db(home, [{}])
    calls = record_connects(viewer, monkeypatch)

    assert fetch(port, "/runs/")[0] == 404
    assert calls == []

    assert fetch(port, "/runs")[0] == 200
    assert_one_read_only_connect(calls)


def test_get_runs_opens_one_read_only_connection_per_request(
    viewer, home, port, monkeypatch
):
    make_runs_db(home, [{}, {}])
    calls = record_connects(viewer, monkeypatch)

    status, _, body = fetch(port, "/runs")

    assert status == 200
    assert body.decode("utf-8").count('<tr id="run-') == 2
    assert_one_read_only_connect(calls)


def test_get_runs_shows_a_run_inserted_between_two_requests(home, port):
    path = make_runs_db(home, [{}])
    _, _, first = fetch(port, "/runs")
    assert 'id="run-1"' in first.decode("utf-8")
    assert 'id="run-2"' not in first.decode("utf-8")

    insert_full_run(path, request="A newer run", **RUNNING)

    status, _, second = fetch(port, "/runs")
    text = second.decode("utf-8")
    assert status == 200
    assert re.findall(r'<tr id="run-(\d+)">', text) == ["2", "1"]
    assert request_cell("A newer run") in text


def test_get_runs_shows_the_outcome_once_a_running_run_has_ended(home, port):
    path = make_runs_db(home, [RUNNING])
    _, _, first = fetch(port, "/runs")
    assert '<td class="run-status">working</td>' in first.decode("utf-8")

    execute_sql(
        path,
        "UPDATE runs SET state = 'ended', outcome = 'needs-me', ended_at = ?",
        (ENDED,),
    )

    _, _, second = fetch(port, "/runs")
    text = second.decode("utf-8")
    assert run_row(1, "needs-me", "Fix the bug", CREATED_TEXT, "3m 7s") in text
    assert "so far" not in text


def test_get_runs_twice_returns_the_same_page_when_every_run_has_ended(home, port):
    make_runs_db(home, [{}, {}])
    first = fetch(port, "/runs")
    second = fetch(port, "/runs")
    assert first[0] == second[0] == 200
    assert first[2] == second[2]
    assert b'<table id="runs">' in first[2]


def test_get_runs_with_an_empty_runs_table_says_no_runs_recorded_yet(
    viewer, home, port
):
    make_runs_db(home)
    status, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert text == RUNS_HEAD + NO_RUNS_LINE + DOCUMENT_TAIL


def test_get_runs_without_database_returns_200_with_runs_unavailable(home, port):
    status, headers, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert text == RUNS_HEAD + RUNS_UNAVAILABLE_LINE + DOCUMENT_TAIL
    assert int(headers["Content-Length"]) == len(body)


def test_get_runs_without_database_does_not_create_it(home, port):
    status, _, body = fetch(port, "/runs")
    assert status == 200
    assert RUNS_UNAVAILABLE_LINE in body.decode("utf-8")
    assert not (home / "factory.db").exists()
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("break_database", UNREADABLE_RUNS_DATABASES)
def test_get_runs_returns_200_with_runs_unavailable_when(home, port, break_database):
    break_database(home)
    status, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert RUNS_UNAVAILABLE_LINE in text
    assert "<table" not in text
    assert "no-runs" not in text


def test_get_runs_recovers_once_the_database_appears(home, port):
    _, _, before = fetch(port, "/runs")
    assert RUNS_UNAVAILABLE_LINE in before.decode("utf-8")

    make_runs_db(home, [{}])

    _, _, after = fetch(port, "/runs")
    text = after.decode("utf-8")
    assert run_row(1, "accepted", "Fix the bug") in text
    assert "unavailable" not in text


def test_get_runs_leaves_the_database_file_unchanged(home, port):
    path = make_runs_db(home, [{}, RUNNING])
    before = path.read_bytes()
    status, _, body = fetch(port, "/runs")
    assert status == 200
    assert body.decode("utf-8").count('<tr id="run-') == 2
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_get_runs_does_not_show_the_home_page(home, port):
    make_runs_db(home, [{}])
    status, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert "<title>Runs - tomfactory viewer</title>" in text
    assert "Hello from the factory" not in text
    assert "run-count" not in text


def test_server_still_serves_runs_after_a_404_and_after_the_home_page(home, port):
    make_runs_db(home, [{}])
    assert fetch(port, "/nope")[0] == 404
    assert fetch(port, "/")[0] == 200
    status, _, body = fetch(port, "/runs")
    assert status == 200
    assert b'id="run-1"' in body


# --------------------------------------------- HTTP server, the link from the home page


def test_get_root_links_to_the_runs_page(home, port):
    make_runs_db(home, [{}])
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert 'href="/runs"' in text
    assert "<h1>Hello from the factory</h1>\n" + RUNS_LINK_LINE + COUNT_LINE.format(1) in text


def test_get_root_links_to_the_runs_page_when_there_is_no_database(home, port):
    status, _, body = fetch(port)
    assert status == 200
    assert RUNS_LINK_LINE in body.decode("utf-8")


def test_the_runs_link_on_the_home_page_leads_to_the_runs_page(home, port):
    make_runs_db(home, [{"run_id": 7}])
    _, _, home_body = fetch(port)
    match = re.search(r'<a id="runs-link" href="([^"]+)">', home_body.decode("utf-8"))
    assert match is not None
    status, _, runs_body = fetch(port, match.group(1))
    assert status == 200
    assert 'id="run-7"' in runs_body.decode("utf-8")
