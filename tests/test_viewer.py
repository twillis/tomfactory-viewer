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
    make_runs_db(home, [{"request": "Fraction", "created_at": stored}])
    (run,) = viewer.read_runs_page_data().runs
    assert run.created_at == CREATED_DT.replace(microsecond=microsecond)
    assert run.created_at.utcoffset() == timedelta(0)
    assert run.ended_at == ENDED_DT


@pytest.mark.parametrize("stored, microsecond", FRACTIONAL_TIMES)
def test_read_runs_page_data_parses_ended_at_keeping_the_fraction_of(
    viewer, home, stored, microsecond
):
    make_runs_db(home, [{"request": "Fraction", "ended_at": stored}])
    (run,) = viewer.read_runs_page_data().runs
    assert run.ended_at == CREATED_DT.replace(microsecond=microsecond)
    assert run.ended_at.utcoffset() == timedelta(0)
    assert run.created_at == CREATED_DT


def test_get_runs_shows_created_and_duration_of_a_run_stored_with_milliseconds(
    home, port
):
    make_runs_db(
        home,
        [
            {
                "created_at": "2026-11-18T21:04:05.123Z",
                "ended_at": "2026-11-18T21:07:12.623Z",
            }
        ],
    )
    status, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert '<td class="run-created">2026-11-18 21:04:05 UTC</td>' in text
    assert duration_cell("3m 7s") in text
    assert "unknown" not in text


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
        r'<tr id="run-2"><td class="run-id"><a href="/runs/2">2</a></td>'
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
    "path", ["/runs/", "/runs.html", "/run", "/Runs", "/runs/?x=1"]
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
        (
            run_id,
            request,
            target,
            "abc123",
            branch,
            state,
            outcome,
            reason,
            attempt_limit,
            created_at,
            ended_at,
        ),
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
        (
            step_id,
            run_id,
            name,
            kind,
            persona,
            attempt,
            "do the work",
            checks,
            passed,
            started_at,
            ended_at,
        ),
    )


def make_run_db(home, runs=(), steps=()):
    """Create ``home/factory.db`` with the documented ``runs`` and ``steps`` tables.

    ``runs`` and ``steps`` are sequences of keyword-argument mappings for
    ``insert_detail_run`` and ``insert_step``, inserted in order.
    """
    home.mkdir(parents=True, exist_ok=True)
    path = home / "factory.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(RUNS_DDL + STEPS_DDL)
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


def step_duration_cell(text):
    return f'<td class="step-duration">{text}</td>'


def step_checks_cell(text):
    return f'<td class="step-checks">{text}</td>'


def _make_runs_table_without_target(home):
    path = make_db(home, with_runs_table=False)
    execute_sql(
        path,
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, state TEXT, outcome TEXT, "
        "request TEXT, created_at TEXT, ended_at TEXT, branch TEXT, reason TEXT, "
        "attempt_limit INTEGER)",
    )
    execute_sql(
        path,
        "INSERT INTO runs (id, state, outcome, request, created_at, ended_at, "
        "attempt_limit) VALUES (7, 'ended', 'accepted', 'Fix the bug', ?, ?, 3)",
        (CREATED, ENDED),
    )


def _make_runs_table_without_attempt_limit(home):
    path = make_db(home, with_runs_table=False)
    execute_sql(
        path,
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, state TEXT, outcome TEXT, "
        "request TEXT, created_at TEXT, ended_at TEXT, target TEXT, branch TEXT, "
        "reason TEXT)",
    )
    execute_sql(
        path,
        "INSERT INTO runs (id, state, outcome, request, created_at, ended_at, target) "
        "VALUES (7, 'ended', 'accepted', 'Fix the bug', ?, ?, '/tmp/target')",
        (CREATED, ENDED),
    )


# Ways for the run itself to be unreadable. Each takes the home directory.
UNREADABLE_RUN_DATABASES = UNREADABLE_RUNS_DATABASES + [
    pytest.param(_make_runs_table_without_target, id="runs_has_no_target_column"),
    pytest.param(
        _make_runs_table_without_attempt_limit, id="runs_has_no_attempt_limit_column"
    ),
]


def _make_run_without_steps_table(home):
    path = make_runs_db(home)
    insert_detail_run(path)


LOOSE_STEP_COLUMNS = {
    "name": "TEXT",
    "persona": "TEXT",
    "attempt": "INTEGER",
    "started_at": "TEXT",
    "ended_at": "TEXT",
    "passed": "BOOLEAN",
    "checks": "TEXT",
}


def _make_steps_table_without(column):
    def make(home):
        path = make_runs_db(home)
        insert_detail_run(path)
        columns = ", ".join(
            f"{name} {kind}"
            for name, kind in LOOSE_STEP_COLUMNS.items()
            if name != column
        )
        execute_sql(
            path,
            f"CREATE TABLE steps (id INTEGER PRIMARY KEY, run_id INTEGER, {columns})",
        )
        execute_sql(path, "INSERT INTO steps (run_id) VALUES (7)")

    return make


# Ways for the steps to be unreadable while run 7 itself can be read.
UNREADABLE_STEPS_DATABASES = [
    pytest.param(_make_run_without_steps_table, id="steps_table_is_missing"),
    pytest.param(_make_steps_table_without("checks"), id="steps_has_no_checks_column"),
    pytest.param(
        _make_steps_table_without("persona"), id="steps_has_no_persona_column"
    ),
    pytest.param(_make_steps_table_without("passed"), id="steps_has_no_passed_column"),
    pytest.param(
        _make_steps_table_without("started_at"), id="steps_has_no_started_at_column"
    ),
]


def fail_statements_on_steps(sql):
    """An ``after_execute`` hook for ``wrap_connections``: reading ``steps`` fails."""
    if "steps" in sql.lower():
        raise sqlite3.OperationalError("database is locked")


# ----------------------------------------------------------- the run page's value types


def test_check_has_name_and_passed_fields_in_that_order(viewer):
    check = viewer.Check("tests", False)
    assert viewer.Check._fields == ("name", "passed")
    assert check.name == "tests"
    assert check.passed is False


def test_step_summary_has_its_eight_fields_in_order(viewer):
    assert viewer.StepSummary._fields == (
        "id",
        "name",
        "persona",
        "attempt",
        "started_at",
        "ended_at",
        "passed",
        "checks",
    )


def test_step_summary_keeps_the_values_it_is_given(viewer):
    checks = (viewer.Check("guard", True),)
    step = viewer.StepSummary(
        12, "test", "tester", 2, CREATED_DT, ENDED_DT, False, checks
    )
    assert step.id == 12
    assert step.name == "test"
    assert step.persona == "tester"
    assert step.attempt == 2
    assert step.started_at == CREATED_DT
    assert step.ended_at == ENDED_DT
    assert step.passed is False
    assert step.checks == checks


def test_run_detail_has_summary_target_branch_reason_and_attempt_limit_in_order(
    viewer,
):
    summary = viewer.RunSummary(7, "ended", "accepted", "Fix", CREATED_DT, ENDED_DT)
    detail = viewer.RunDetail(summary, "/tmp/target", "factory/7-fix", "done", 3)
    assert viewer.RunDetail._fields == (
        "summary",
        "target",
        "branch",
        "reason",
        "attempt_limit",
    )
    assert detail.summary is summary
    assert detail.target == "/tmp/target"
    assert detail.branch == "factory/7-fix"
    assert detail.reason == "done"
    assert detail.attempt_limit == 3


def test_run_detail_states_ended_and_status_through_its_run_summary(viewer):
    assert run_detail(viewer, outcome="rejected").summary.ended is True
    assert run_detail(viewer, outcome="rejected").summary.status == "rejected"
    assert running_detail(viewer, state="planning").summary.ended is False
    assert running_detail(viewer, state="planning").summary.status == "planning"


def test_run_page_data_has_run_id_run_steps_and_read_at_fields_in_that_order(viewer):
    data = viewer.RunPageData(99, None, (), CREATED_DT)
    assert viewer.RunPageData._fields == ("run_id", "run", "steps", "read_at")
    assert data.run_id == 99
    assert data.run is None
    assert data.steps == ()
    assert data.read_at == CREATED_DT


def test_run_page_data_requires_read_at_to_be_given(viewer):
    run_page_data = viewer.RunPageData
    with pytest.raises(TypeError):
        run_page_data(7, None, ())


# ----------------------------------------------------------- StepSummary: finished


def test_step_summary_that_passed_has_finished(viewer):
    assert step_summary(viewer, passed=True).finished is True


def test_step_summary_that_failed_has_finished(viewer):
    assert step_summary(viewer, passed=False).finished is True


def test_step_summary_with_no_result_has_not_finished(viewer):
    assert unfinished_step(viewer).finished is False


def test_step_summary_finished_is_decided_by_passed_not_by_ended_at(viewer):
    result_without_end = step_summary(viewer, passed=False, ended_at=None)
    assert result_without_end.finished is True

    end_without_result = step_summary(viewer, passed=None, ended_at=ENDED_DT)
    assert end_without_result.finished is False


# ----------------------------------------------------------- StepSummary: duration


def test_step_summary_duration_of_a_finished_step_is_from_started_to_ended(viewer):
    step = step_summary(viewer)
    assert step.duration(ENDED_DT) == timedelta(seconds=187)


def test_step_summary_duration_of_a_failed_step_is_from_started_to_ended(viewer):
    step = step_summary(viewer, passed=False)
    assert step.duration(ENDED_DT) == timedelta(seconds=187)


def test_step_summary_duration_of_a_finished_step_ignores_now(viewer):
    step = step_summary(viewer)
    assert step.duration(ENDED_DT + timedelta(days=400)) == timedelta(seconds=187)
    assert step.duration(CREATED_DT - timedelta(days=1)) == timedelta(seconds=187)


def test_step_summary_duration_of_an_unfinished_step_is_from_started_to_now(viewer):
    step = unfinished_step(viewer)
    assert step.duration(CREATED_DT + timedelta(hours=2, seconds=7)) == timedelta(
        hours=2, seconds=7
    )


def test_step_summary_duration_of_an_unfinished_step_grows_with_now(viewer):
    step = unfinished_step(viewer)
    earlier = step.duration(CREATED_DT + timedelta(seconds=10))
    later = step.duration(CREATED_DT + timedelta(seconds=11))
    assert later - earlier == timedelta(seconds=1)


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


def test_step_summary_duration_is_none_for_an_unfinished_step_without_started_at(
    viewer,
):
    step = unfinished_step(viewer, started_at=None)
    assert step.duration(ENDED_DT) is None


def test_step_summary_duration_is_none_for_a_finished_step_without_ended_at(viewer):
    step = step_summary(viewer, ended_at=None)
    assert step.duration(ENDED_DT + timedelta(days=1)) is None


def test_step_summary_duration_is_none_when_both_times_are_missing(viewer):
    step = step_summary(viewer, started_at=None, ended_at=None)
    assert step.duration(ENDED_DT) is None


def test_step_summary_duration_is_zero_when_a_step_ended_before_it_started(viewer):
    step = step_summary(viewer, ended_at=CREATED_DT - timedelta(seconds=30))
    assert step.duration(ENDED_DT) == timedelta(0)


def test_step_summary_duration_is_zero_when_now_is_before_an_unfinished_step_started(
    viewer,
):
    step = unfinished_step(viewer)
    assert step.duration(CREATED_DT - timedelta(seconds=30)) == timedelta(0)


def test_step_summary_duration_is_zero_when_a_step_ended_the_moment_it_started(viewer):
    step = step_summary(viewer, ended_at=CREATED_DT)
    assert step.duration(ENDED_DT) == timedelta(0)


# ----------------------------------------------------- read_run_page_data: the run


def test_read_run_page_data_returns_a_run_page_data(viewer, home):
    make_run_db(home, runs=[{}])
    assert isinstance(viewer.read_run_page_data(7), viewer.RunPageData)


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


def test_read_run_page_data_run_is_a_run_detail_holding_a_run_summary(viewer, home):
    make_run_db(home, runs=[{}])
    run = viewer.read_run_page_data(7).run
    assert isinstance(run, viewer.RunDetail)
    assert isinstance(run.summary, viewer.RunSummary)
    assert type(run.summary.id) is int
    assert type(run.attempt_limit) is int


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


def test_read_run_page_data_run_id_is_the_id_asked_for(viewer, home):
    make_run_db(home, runs=[{"run_id": 3}])
    assert viewer.read_run_page_data(3).run_id == 3
    assert viewer.read_run_page_data(99).run_id == 99


def test_read_run_page_data_gives_a_running_run_empty_outcome_and_no_ended_at(
    viewer, home
):
    make_run_db(home, runs=[{"reason": None, **RUNNING}])
    run = viewer.read_run_page_data(7).run
    assert run == running_detail(viewer)
    assert run.summary.outcome == ""
    assert run.summary.ended_at is None
    assert run.summary.ended is False


@pytest.mark.parametrize("state", ["planning", "awaiting_approval", "working"])
def test_read_run_page_data_returns_the_stored_state_of_a_run_that_has_not_ended(
    viewer, home, state
):
    make_run_db(home, runs=[{"state": state, "outcome": None, "ended_at": None}])
    run = viewer.read_run_page_data(7).run
    assert run.summary.state == state
    assert run.summary.status == state


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "needs-me"])
def test_read_run_page_data_returns_the_stored_outcome_of_an_ended_run(
    viewer, home, outcome
):
    make_run_db(home, runs=[{"outcome": outcome}])
    run = viewer.read_run_page_data(7).run
    assert run.summary.outcome == outcome
    assert run.summary.status == outcome


def test_read_run_page_data_branch_is_an_empty_string_when_null(viewer, home):
    make_run_db(home, runs=[{"branch": None}])
    run = viewer.read_run_page_data(7).run
    assert run.branch == ""
    assert run == run_detail(viewer, branch="")


def test_read_run_page_data_reason_is_an_empty_string_when_null(viewer, home):
    make_run_db(home, runs=[{"reason": None}])
    run = viewer.read_run_page_data(7).run
    assert run.reason == ""
    assert run == run_detail(viewer, reason="")


def test_read_run_page_data_keeps_the_reason_of_a_run_that_has_not_ended(viewer, home):
    make_run_db(home, runs=[{"reason": "still going", **RUNNING}])
    assert viewer.read_run_page_data(7).run.reason == "still going"


def test_read_run_page_data_returns_the_stored_attempt_limit(viewer, home):
    make_run_db(home, runs=[{"attempt_limit": 5}])
    assert viewer.read_run_page_data(7).run.attempt_limit == 5


def test_read_run_page_data_returns_text_in_full_and_unchanged(viewer, home):
    request = "  <b>café</b> & 'quotes' ✓\n\nsecond paragraph " + "word " * 200
    make_run_db(
        home,
        runs=[
            {
                "request": request,
                "target": "/tmp/a&b <target>",
                "branch": "factory/7-<b>",
                "reason": "it's \"bad\" & <i>worse</i>",
            }
        ],
        steps=[{"name": "<test>", "persona": "te&ster"}],
    )
    data = viewer.read_run_page_data(7)
    assert data.run.summary.request == request
    assert data.run.target == "/tmp/a&b <target>"
    assert data.run.branch == "factory/7-<b>"
    assert data.run.reason == "it's \"bad\" & <i>worse</i>"
    assert data.steps[0].name == "<test>"
    assert data.steps[0].persona == "te&ster"


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


def test_read_run_page_data_steps_is_a_tuple_of_step_summaries(viewer, home):
    make_run_db(home, runs=[{}], steps=[{}, UNFINISHED])
    steps = viewer.read_run_page_data(7).steps
    assert type(steps) is tuple
    assert len(steps) == 2
    assert all(isinstance(step, viewer.StepSummary) for step in steps)
    assert all(type(step.id) is int for step in steps)
    assert all(type(step.attempt) is int for step in steps)


def test_read_run_page_data_gives_an_empty_tuple_for_a_run_with_no_steps(viewer, home):
    make_run_db(home, runs=[{}])
    data = viewer.read_run_page_data(7)
    assert data.run == run_detail(viewer)
    assert data.steps == ()
    assert type(data.steps) is tuple
    assert data.steps is not viewer.UNREADABLE


def test_read_run_page_data_returns_a_single_step(viewer, home):
    make_run_db(home, runs=[{}], steps=[{"step_id": 12}])
    assert viewer.read_run_page_data(7).steps == (step_summary(viewer),)


def test_read_run_page_data_returns_steps_lowest_id_first(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[{"name": "plan"}, {"name": "red"}, {"name": "build"}],
    )
    steps = viewer.read_run_page_data(7).steps
    assert [step.id for step in steps] == [1, 2, 3]
    assert [step.name for step in steps] == ["plan", "red", "build"]


def test_read_run_page_data_orders_steps_by_id_not_by_insertion_order(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[{"step_id": 30}, {"step_id": 4}, {"step_id": 200}, {"step_id": 12}],
    )
    steps = viewer.read_run_page_data(7).steps
    assert [step.id for step in steps] == [4, 12, 30, 200]


def test_read_run_page_data_orders_steps_by_id_not_by_started_at(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {"step_id": 1, "started_at": "2030-01-01T00:00:00Z"},
            {"step_id": 2, "started_at": "2020-01-01T00:00:00Z"},
        ],
    )
    assert [step.id for step in viewer.read_run_page_data(7).steps] == [1, 2]


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


def test_read_run_page_data_gives_no_steps_to_a_run_when_only_others_have_them(
    viewer, home
):
    make_run_db(
        home, runs=[{"run_id": 6}, {"run_id": 7}], steps=[{"run_id": 6}, {"run_id": 6}]
    )
    assert viewer.read_run_page_data(7).steps == ()
    assert len(viewer.read_run_page_data(6).steps) == 2


def test_read_run_page_data_returns_every_attempt_of_a_step(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {"name": "test", "attempt": 1, "passed": 0},
            {"name": "test", "attempt": 2, "passed": 0},
            {"name": "test", "attempt": 3, "passed": 1},
        ],
    )
    steps = viewer.read_run_page_data(7).steps
    assert [(step.name, step.attempt, step.passed) for step in steps] == [
        ("test", 1, False),
        ("test", 2, False),
        ("test", 3, True),
    ]


def test_read_run_page_data_returns_every_step_when_there_are_many(viewer, home):
    path = make_run_db(home, runs=[{}])
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO steps (run_id, name, kind, attempt, asked, passed, "
            "started_at, ended_at) VALUES (7, ?, 'code', 1, 'do the work', 1, ?, ?)",
            [(f"step number {n}", CREATED, ENDED) for n in range(1, 301)],
        )
        conn.commit()
    finally:
        conn.close()

    steps = viewer.read_run_page_data(7).steps
    assert len(steps) == 300
    assert [step.id for step in steps] == list(range(1, 301))
    assert steps[0].name == "step number 1"
    assert steps[-1].name == "step number 300"


def test_read_run_page_data_persona_is_an_empty_string_when_null(viewer, home):
    make_run_db(home, runs=[{}], steps=[{"step_id": 12, "persona": None}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.persona == ""
    assert step == step_summary(viewer, persona="")


def test_read_run_page_data_passed_is_true_when_stored_as_one(viewer, home):
    make_run_db(home, runs=[{}], steps=[{"passed": 1}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.passed is True
    assert step.finished is True


def test_read_run_page_data_passed_is_false_when_stored_as_zero(viewer, home):
    make_run_db(home, runs=[{}], steps=[{"passed": 0}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.passed is False
    assert step.finished is True


def test_read_run_page_data_passed_is_none_when_null(viewer, home):
    make_run_db(home, runs=[{}], steps=[UNFINISHED])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.passed is None
    assert step.finished is False


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


def test_read_run_page_data_checks_is_an_empty_tuple_when_null(viewer, home):
    make_run_db(home, runs=[{}], steps=[{"checks": None}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == ()
    assert type(step.checks) is tuple


def test_read_run_page_data_checks_is_an_empty_tuple_for_an_empty_json_list(
    viewer, home
):
    make_run_db(home, runs=[{}], steps=[{"checks": "[]"}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == ()
    assert step.checks is not viewer.UNREADABLE


def test_read_run_page_data_checks_is_a_tuple_of_checks(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[{"checks": checks_json(("guard", True), ("tests", False))}],
    )
    (step,) = viewer.read_run_page_data(7).steps
    assert type(step.checks) is tuple
    assert all(isinstance(check, viewer.Check) for check in step.checks)
    assert step.checks[0].passed is True
    assert step.checks[1].passed is False


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


def test_read_run_page_data_reads_a_check_that_timed_out_with_a_note(viewer, home):
    stored = json.dumps(
        [
            {
                "name": "tests",
                "passed": False,
                "note": "gave up after 600s",
                "timed_out": True,
            }
        ]
    )
    make_run_db(home, runs=[{}], steps=[{"checks": stored, "passed": 0}])
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == (viewer.Check("tests", False),)


def test_read_run_page_data_reads_a_check_that_has_only_a_name_and_passed(
    viewer, home
):
    make_run_db(
        home, runs=[{}], steps=[{"checks": '[{"name": "guard", "passed": true}]'}]
    )
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == (viewer.Check("guard", True),)


def test_read_run_page_data_returns_check_names_unchanged(viewer, home):
    make_run_db(
        home,
        runs=[{}],
        steps=[{"checks": checks_json(("<b>café</b> & ✓", True), ("", False))}],
    )
    (step,) = viewer.read_run_page_data(7).steps
    assert step.checks == (
        viewer.Check("<b>café</b> & ✓", True),
        viewer.Check("", False),
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


@pytest.mark.parametrize("stored", BAD_CHECKS)
def test_read_run_page_data_keeps_the_other_steps_when_one_has_checks_stored_as(
    viewer, home, stored
):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {"step_id": 1, "checks": checks_json(("guard", True))},
            {"step_id": 2, "checks": stored, "passed": 0},
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


# ----------------------------------------------- read_run_page_data: timestamps


def test_read_run_page_data_parses_timestamps_as_utc_aware_datetimes(viewer, home):
    make_run_db(home, runs=[{}], steps=[{}])
    data = viewer.read_run_page_data(7)
    (step,) = data.steps
    for parsed in (
        data.run.summary.created_at,
        data.run.summary.ended_at,
        step.started_at,
        step.ended_at,
    ):
        assert isinstance(parsed, datetime)
        assert parsed.utcoffset() == timedelta(0)
    assert step.started_at == CREATED_DT
    assert step.ended_at == ENDED_DT


def test_read_run_page_data_parses_step_times_with_milliseconds_keeping_them(
    viewer, home
):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {
                "started_at": "2026-11-18T21:04:05.123Z",
                "ended_at": "2026-11-18T21:07:12.623Z",
            }
        ],
    )
    (step,) = viewer.read_run_page_data(7).steps
    assert step.started_at == CREATED_DT.replace(microsecond=123000)
    assert step.ended_at == ENDED_DT.replace(microsecond=623000)
    assert step.started_at.utcoffset() == timedelta(0)


def test_read_run_page_data_parses_run_times_with_milliseconds_keeping_them(
    viewer, home
):
    make_run_db(
        home,
        runs=[
            {
                "created_at": "2026-11-18T21:04:05.250Z",
                "ended_at": "2026-11-18T21:07:12.123Z",
            }
        ],
    )
    summary = viewer.read_run_page_data(7).run.summary
    assert summary.created_at == CREATED_DT.replace(microsecond=250000)
    assert summary.ended_at == ENDED_DT.replace(microsecond=123000)


STEP_TIME_FORMS = [
    pytest.param(CREATED, ENDED, timedelta(seconds=187), id="both_in_whole_seconds"),
    pytest.param(
        "2026-11-18T21:04:05.123Z",
        "2026-11-18T21:07:12.623Z",
        timedelta(seconds=187, milliseconds=500),
        id="both_with_milliseconds",
    ),
    pytest.param(
        CREATED,
        "2026-11-18T21:07:12.250Z",
        timedelta(seconds=187, milliseconds=250),
        id="whole_seconds_then_milliseconds",
    ),
    pytest.param(
        "2026-11-18T21:04:05.500Z",
        ENDED,
        timedelta(seconds=186, milliseconds=500),
        id="milliseconds_then_whole_seconds",
    ),
]


@pytest.mark.parametrize("started, ended, expected", STEP_TIME_FORMS)
def test_read_run_page_data_gives_the_duration_of_a_step_stored_with(
    viewer, home, started, ended, expected
):
    make_run_db(home, runs=[{}], steps=[{"started_at": started, "ended_at": ended}])
    data = viewer.read_run_page_data(7)
    (step,) = data.steps
    assert step.duration(data.read_at) == expected


def test_read_run_page_data_gives_the_duration_so_far_of_an_unfinished_step_stored_with_milliseconds(
    viewer, home
):
    make_run_db(
        home,
        runs=[{"reason": None, **RUNNING}],
        steps=[{"started_at": "2026-11-18T21:04:05.123Z", **UNFINISHED}],
    )
    (step,) = viewer.read_run_page_data(7).steps
    now = CREATED_DT + timedelta(seconds=10, milliseconds=123)
    assert step.duration(now) == timedelta(seconds=10)


MALFORMED_STEP_TIMES = MALFORMED_TIMES + [
    pytest.param("2026-11-18T21:04:05.Z", id="a_dot_and_no_fraction"),
    pytest.param("2026-11-18T21:04:05.1234567Z", id="seven_fraction_digits"),
    pytest.param("2026-11-18T21:04:05.123", id="a_fraction_and_no_z"),
    pytest.param("2026-11-18T21:04:05,123Z", id="a_comma_before_the_fraction"),
]


@pytest.mark.parametrize("stored", MALFORMED_STEP_TIMES)
def test_read_run_page_data_step_started_at_is_none_when_stored_as(
    viewer, home, stored
):
    make_run_db(home, runs=[{}], steps=[{"step_id": 12, "started_at": stored}])
    data = viewer.read_run_page_data(7)
    assert data.steps == (step_summary(viewer, started_at=None),)
    assert data.steps[0].duration(data.read_at) is None


@pytest.mark.parametrize("stored", MALFORMED_STEP_TIMES)
def test_read_run_page_data_step_ended_at_is_none_when_stored_as(viewer, home, stored):
    make_run_db(home, runs=[{}], steps=[{"step_id": 12, "ended_at": stored}])
    data = viewer.read_run_page_data(7)
    assert data.steps == (step_summary(viewer, ended_at=None),)
    assert data.steps[0].duration(data.read_at) is None


def test_read_run_page_data_run_created_at_is_none_when_malformed(viewer, home):
    make_run_db(home, runs=[{"created_at": "yesterday"}])
    summary = viewer.read_run_page_data(7).run.summary
    assert summary.created_at is None
    assert summary.ended_at == ENDED_DT


def test_read_run_page_data_keeps_other_steps_when_one_has_a_malformed_time(
    viewer, home
):
    make_run_db(home, runs=[{}], steps=[{}, {"started_at": "garbage"}, {}])
    steps = viewer.read_run_page_data(7).steps
    assert [step.id for step in steps] == [1, 2, 3]
    assert [step.started_at for step in steps] == [CREATED_DT, None, CREATED_DT]


# ---------------------------------------------- read_run_page_data: an unknown id


def test_read_run_page_data_run_is_none_and_steps_empty_for_an_unknown_id(viewer, home):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{"run_id": 7}])
    data = viewer.read_run_page_data(99)
    assert data.run is None
    assert data.run is not viewer.UNREADABLE
    assert data.steps == ()
    assert type(data.steps) is tuple
    assert data.run_id == 99


def test_read_run_page_data_run_is_none_for_any_id_in_an_empty_runs_table(viewer, home):
    make_run_db(home)
    data = viewer.read_run_page_data(1)
    assert data.run is None
    assert data.steps == ()


@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param(6, id="one_below_the_only_run"),
        pytest.param(8, id="one_above_the_only_run"),
        pytest.param(999999999999999999, id="the_largest_id_the_page_accepts"),
    ],
)
def test_read_run_page_data_run_is_none_for_an_id_that(viewer, home, run_id):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{"run_id": 7}])
    data = viewer.read_run_page_data(run_id)
    assert data.run is None
    assert data.steps == ()
    assert data.run_id == run_id


def test_read_run_page_data_does_not_return_steps_that_name_a_run_with_no_row(
    viewer, home
):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{"run_id": 99}, {"run_id": 99}])
    data = viewer.read_run_page_data(99)
    assert data.run is None
    assert data.steps == ()


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


def test_read_run_page_data_runs_a_statement_on_steps_for_a_known_id(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{"run_id": 7}], steps=[{"run_id": 7}])
    proxies = wrap_connections(viewer, monkeypatch)

    data = viewer.read_run_page_data(7)

    assert len(data.steps) == 1
    assert len(proxies) == 1
    assert sum("steps" in sql.lower() for sql in proxies[0].statements) == 1


def test_read_run_page_data_steps_are_empty_not_unreadable_for_an_unknown_id_without_a_steps_table(
    viewer, home
):
    _make_run_without_steps_table(home)
    data = viewer.read_run_page_data(99)
    assert data.run is None
    assert data.steps == ()
    assert data.steps is not viewer.UNREADABLE


# ------------------------------------------------- read_run_page_data: unreadable


@pytest.mark.parametrize("break_database", UNREADABLE_RUN_DATABASES)
def test_read_run_page_data_run_and_steps_are_unreadable_when(
    viewer, home, break_database
):
    break_database(home)
    data = viewer.read_run_page_data(7)
    assert isinstance(data, viewer.RunPageData)
    assert data.run is viewer.UNREADABLE
    assert data.steps is viewer.UNREADABLE
    assert data.run_id == 7


@pytest.mark.parametrize("break_database", UNREADABLE_RUN_DATABASES)
def test_read_run_page_data_never_reports_run_not_found_when(
    viewer, home, break_database
):
    break_database(home)
    data = viewer.read_run_page_data(99)
    assert data.run is not None
    assert data.run is viewer.UNREADABLE
    assert data.steps != ()


def test_read_run_page_data_run_and_steps_are_unreadable_when_home_directory_is_missing(
    viewer, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOMFACTORY_HOME", str(tmp_path / "does-not-exist"))
    data = viewer.read_run_page_data(7)
    assert data.run is viewer.UNREADABLE
    assert data.steps is viewer.UNREADABLE


def test_read_run_page_data_run_and_steps_are_unreadable_when_connect_raises(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}])

    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(viewer.sqlite3, "connect", failing_connect)
    data = viewer.read_run_page_data(7)
    assert data.run is viewer.UNREADABLE
    assert data.steps is viewer.UNREADABLE


def test_read_run_page_data_run_and_steps_are_unreadable_when_every_statement_fails(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}])
    made = break_connections(viewer, monkeypatch, fail_on="execute")

    data = viewer.read_run_page_data(7)

    assert data.run is viewer.UNREADABLE
    assert data.steps is viewer.UNREADABLE
    assert len(made) == 1
    assert made[0].close_calls == 1


def test_read_run_page_data_run_and_steps_are_unreadable_when_fetching_the_run_fails(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}])
    made = break_connections(viewer, monkeypatch, fail_on="fetch")

    data = viewer.read_run_page_data(7)

    assert data.run is viewer.UNREADABLE
    assert data.steps is viewer.UNREADABLE
    assert len(made) == 1
    assert made[0].close_calls == 1


@pytest.mark.parametrize("break_steps", UNREADABLE_STEPS_DATABASES)
def test_read_run_page_data_keeps_the_run_and_marks_steps_unreadable_when(
    viewer, home, break_steps
):
    break_steps(home)
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


# --------------------------------------------------- read_run_page_data: read_at


def test_read_run_page_data_read_at_is_the_utc_time_of_the_call(viewer, home):
    make_run_db(home, runs=[{}], steps=[{}])
    before = datetime.now(timezone.utc)
    data = viewer.read_run_page_data(7)
    after = datetime.now(timezone.utc)
    assert data.read_at.utcoffset() == timedelta(0)
    assert before <= data.read_at <= after


def test_read_run_page_data_read_at_is_set_when_nothing_is_readable(viewer, home):
    before = datetime.now(timezone.utc)
    data = viewer.read_run_page_data(7)
    after = datetime.now(timezone.utc)
    assert data.run is viewer.UNREADABLE
    assert data.read_at.utcoffset() == timedelta(0)
    assert before <= data.read_at <= after


def test_read_run_page_data_read_at_is_set_for_an_unknown_id(viewer, home):
    make_run_db(home, runs=[{}])
    before = datetime.now(timezone.utc)
    data = viewer.read_run_page_data(99)
    after = datetime.now(timezone.utc)
    assert data.run is None
    assert before <= data.read_at <= after


def test_read_run_page_data_read_at_is_set_when_only_the_steps_are_unreadable(
    viewer, home
):
    _make_run_without_steps_table(home)
    before = datetime.now(timezone.utc)
    data = viewer.read_run_page_data(7)
    after = datetime.now(timezone.utc)
    assert data.steps is viewer.UNREADABLE
    assert before <= data.read_at <= after


# -------------------------------- read_run_page_data: one read-only connection


def test_read_run_page_data_opens_one_read_only_connection_for_run_and_steps(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}, {}])
    calls = record_connects(viewer, monkeypatch)

    data = viewer.read_run_page_data(7)

    assert data.run == run_detail(viewer)
    assert len(data.steps) == 2
    assert_one_read_only_connect(calls)


def test_read_run_page_data_opens_one_read_only_connection_for_an_unknown_id(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}])
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_run_page_data(99).run is None
    assert_one_read_only_connect(calls)


def test_read_run_page_data_tries_to_connect_only_once_when_the_database_is_missing(
    viewer, home, monkeypatch
):
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    assert_one_read_only_connect(calls)


def test_read_run_page_data_opens_one_read_only_connection_when_steps_are_unreadable(
    viewer, home, monkeypatch
):
    _make_run_without_steps_table(home)
    calls = record_connects(viewer, monkeypatch)

    assert viewer.read_run_page_data(7).steps is viewer.UNREADABLE
    assert_one_read_only_connect(calls)


def test_read_run_page_data_opens_one_connection_per_call(viewer, home, monkeypatch):
    make_run_db(home, runs=[{}])
    calls = record_connects(viewer, monkeypatch)

    viewer.read_run_page_data(7)
    viewer.read_run_page_data(7)
    assert len(calls) == 2


def test_read_run_page_data_closes_its_connection_once_after_a_successful_read(
    viewer, home, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}, {}])
    proxies = wrap_connections(viewer, monkeypatch)

    assert len(viewer.read_run_page_data(7).steps) == 2
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


def test_read_run_page_data_closes_its_connection_when_the_runs_table_is_missing(
    viewer, home, monkeypatch
):
    make_db(home, with_runs_table=False)
    proxies = wrap_connections(viewer, monkeypatch)

    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    assert len(proxies) == 1
    assert proxies[0].close_calls == 1


def test_read_run_page_data_closes_its_connection_when_the_steps_table_is_missing(
    viewer, home, monkeypatch
):
    _make_run_without_steps_table(home)
    proxies = wrap_connections(viewer, monkeypatch)

    assert viewer.read_run_page_data(7).steps is viewer.UNREADABLE
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


# ------------------------------ read_run_page_data: never writes or creates


def test_read_run_page_data_does_not_create_a_missing_database_file(viewer, home):
    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    assert not (home / "factory.db").exists()
    assert list(home.iterdir()) == []


def test_read_run_page_data_does_not_create_a_missing_home_directory(
    viewer, tmp_path, monkeypatch
):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("TOMFACTORY_HOME", str(missing))
    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_read_run_page_data_leaves_the_database_file_unchanged(viewer, home):
    path = make_run_db(
        home,
        runs=[{}],
        steps=[{"checks": checks_json(("guard", True))}, {"checks": "not json"}],
    )
    before = path.read_bytes()
    assert len(viewer.read_run_page_data(7).steps) == 2
    assert len(viewer.read_run_page_data(7).steps) == 2
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_run_page_data_leaves_the_database_file_unchanged_for_an_unknown_id(
    viewer, home
):
    path = make_run_db(home, runs=[{}], steps=[{}])
    before = path.read_bytes()
    assert viewer.read_run_page_data(99).run is None
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_run_page_data_leaves_a_database_without_a_steps_table_unchanged(
    viewer, home
):
    _make_run_without_steps_table(home)
    path = home / "factory.db"
    before = path.read_bytes()
    assert viewer.read_run_page_data(7).steps is viewer.UNREADABLE
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_run_page_data_leaves_a_non_sqlite_file_unchanged(viewer, home):
    _write_non_sqlite_file(home)
    path = home / "factory.db"
    before = path.read_bytes()
    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_read_run_page_data_leaves_an_empty_database_file_empty(viewer, home):
    _write_empty_file(home)
    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    assert (home / "factory.db").read_bytes() == b""
    assert [p.name for p in home.iterdir()] == ["factory.db"]


# -------------------------------------------- read_run_page_data: between calls


def test_read_run_page_data_called_twice_returns_the_same_run_and_steps(viewer, home):
    make_run_db(home, runs=[{}], steps=[{}, {"passed": 0}])
    first = viewer.read_run_page_data(7)
    second = viewer.read_run_page_data(7)
    assert first.run == second.run == run_detail(viewer)
    assert first.steps == second.steps
    assert len(first.steps) == 2
    assert first.read_at <= second.read_at


def test_read_run_page_data_sees_a_newly_started_step_on_the_next_call(viewer, home):
    path = make_run_db(home, runs=[{"reason": None, **RUNNING}], steps=[{}])
    assert [step.id for step in viewer.read_run_page_data(7).steps] == [1]
    insert_step(path, name="build", **UNFINISHED)
    steps = viewer.read_run_page_data(7).steps
    assert [step.name for step in steps] == ["test", "build"]
    assert steps[1].finished is False


def test_read_run_page_data_sees_a_step_finishing_on_the_next_call(viewer, home):
    path = make_run_db(
        home, runs=[{"reason": None, **RUNNING}], steps=[{"step_id": 12, **UNFINISHED}]
    )
    (before,) = viewer.read_run_page_data(7).steps
    assert before.passed is None
    assert before.checks == ()

    execute_sql(
        path,
        "UPDATE steps SET passed = 0, ended_at = ?, checks = ? WHERE id = 12",
        (ENDED, checks_json(("tests", False))),
    )

    (after,) = viewer.read_run_page_data(7).steps
    assert after == step_summary(
        viewer, passed=False, checks=(viewer.Check("tests", False),)
    )


def test_read_run_page_data_sees_the_run_ending_on_the_next_call(viewer, home):
    path = make_run_db(home, runs=[{"reason": None, **RUNNING}])
    assert viewer.read_run_page_data(7).run == running_detail(viewer)

    execute_sql(
        path,
        "UPDATE runs SET state = 'ended', outcome = 'needs-me', "
        "reason = 'interrupted', ended_at = ?",
        (ENDED,),
    )

    assert viewer.read_run_page_data(7).run == run_detail(
        viewer, outcome="needs-me", reason="interrupted"
    )


def test_read_run_page_data_finds_a_run_once_it_has_been_inserted(viewer, home):
    path = make_run_db(home)
    assert viewer.read_run_page_data(7).run is None
    insert_detail_run(path)
    assert viewer.read_run_page_data(7).run == run_detail(viewer)


def test_read_run_page_data_recovers_once_the_database_appears(viewer, home):
    assert viewer.read_run_page_data(7).run is viewer.UNREADABLE
    make_run_db(home, runs=[{}], steps=[{}])
    data = viewer.read_run_page_data(7)
    assert data.run == run_detail(viewer)
    assert len(data.steps) == 1


def test_read_run_page_data_steps_become_readable_once_the_steps_table_appears(
    viewer, home
):
    _make_run_without_steps_table(home)
    assert viewer.read_run_page_data(7).steps is viewer.UNREADABLE

    conn = sqlite3.connect(home / "factory.db")
    try:
        conn.executescript(STEPS_DDL)
        conn.commit()
    finally:
        conn.close()

    assert viewer.read_run_page_data(7).steps == ()


def test_read_run_page_data_reads_the_environment_on_every_call(
    viewer, tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    make_run_db(first, runs=[{"request": "from first"}])
    make_run_db(second, runs=[{"request": "from second"}])
    monkeypatch.setenv("TOMFACTORY_HOME", str(first))
    assert viewer.read_run_page_data(7).run.summary.request == "from first"
    monkeypatch.setenv("TOMFACTORY_HOME", str(second))
    assert viewer.read_run_page_data(7).run.summary.request == "from second"


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


def test_render_run_page_shows_the_run_and_step_lines_the_plan_spells_out(viewer):
    run = run_detail(viewer, request="the full request")
    step = step_summary(
        viewer,
        attempt=2,
        passed=False,
        checks=(viewer.Check("guard", True), viewer.Check("tests", False)),
    )
    page = render_run(viewer, run, [step])
    assert (
        "<h1>Run 7</h1>\n"
        '<dl id="run">\n'
        '<dt>Request</dt><dd id="run-request">the full request</dd>\n'
        '<dt>Target</dt><dd id="run-target">/path/to/target</dd>\n'
        '<dt>Branch</dt><dd id="run-branch">factory/7-add-a-page</dd>\n'
        '<dt>Outcome</dt><dd id="run-status">accepted</dd>\n'
        '<dt>Reason</dt><dd id="run-reason">all checks passed</dd>\n'
        '<dt>Attempt limit</dt><dd id="run-attempt-limit">3</dd>\n'
        "</dl>\n"
        '<table id="steps">\n'
        "<thead>\n"
        "<tr><th>Name</th><th>Persona</th><th>Attempt</th><th>Duration</th>"
        "<th>Result</th><th>Checks</th></tr>\n"
        "</thead>\n"
        "<tbody>\n"
        '<tr id="step-12"><td class="step-name">test</td>'
        '<td class="step-persona">tester</td><td class="step-attempt">2</td>'
        '<td class="step-duration">3m 7s</td><td class="step-result">failed</td>'
        '<td class="step-checks"><span class="check">guard</span>, '
        '<span class="check check-failed">tests (failed)</span></td></tr>\n'
        "</tbody>\n"
        "</table>\n"
        "</body>\n"
    ) in page
    assert "<title>Run 7 - tomfactory viewer</title>\n" in page


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


def test_render_run_page_names_the_run_id_in_the_title_and_heading(viewer):
    page = render_run(viewer, run_detail(viewer, run_id=123), run_id=123)
    assert "<title>Run 123 - tomfactory viewer</title>\n" in page
    assert "<h1>Run 123</h1>\n" in page
    assert "Run 7" not in page


def test_render_run_page_names_the_run_id_when_the_run_is_unreadable(viewer):
    page = render_run(viewer, viewer.UNREADABLE, viewer.UNREADABLE, run_id=123)
    assert page == (
        run_head("Run 123 - tomfactory viewer", "Run 123")
        + RUN_UNAVAILABLE_LINE
        + DOCUMENT_TAIL
    )


def test_render_run_page_names_the_id_that_was_not_found(viewer):
    page = render_run(viewer, None, (), run_id=999999999999999999)
    assert run_not_found_line(999999999999999999) in page
    assert "<title>Run not found - tomfactory viewer</title>\n" in page
    assert "<h1>Run not found</h1>\n" in page


def test_render_run_page_for_a_run_not_found_shows_no_run_and_no_steps(viewer):
    page = render_run(viewer, None, (), run_id=99)
    assert 'id="run"' not in page
    assert "<table" not in page
    assert "no-steps" not in page
    assert "unavailable" not in page


def test_render_run_page_for_a_run_not_found_does_not_depend_on_read_at(viewer):
    early = render_run(viewer, None, (), read_at=CREATED_DT, run_id=99)
    late = render_run(viewer, None, (), read_at=ENDED_DT + timedelta(days=9), run_id=99)
    assert early == late
    assert run_not_found_line(99) in early


def test_render_run_page_for_an_unreadable_run_shows_no_steps_line(viewer):
    page = render_run(viewer, viewer.UNREADABLE, viewer.UNREADABLE)
    assert RUN_UNAVAILABLE_LINE in page
    assert "steps-unavailable" not in page
    assert "no-steps" not in page
    assert "run-not-found" not in page
    assert "<table" not in page
    assert 'id="run"' not in page


def test_render_run_page_differs_between_not_found_and_unreadable(viewer):
    not_found = render_run(viewer, None, ())
    unreadable = render_run(viewer, viewer.UNREADABLE, viewer.UNREADABLE)
    assert not_found != unreadable
    assert "run-unavailable" not in not_found
    assert "run-not-found" not in unreadable


def test_render_run_page_differs_between_no_steps_and_unreadable_steps(viewer):
    empty = render_run(viewer, run_detail(viewer), [])
    unreadable = render_run(viewer, run_detail(viewer), viewer.UNREADABLE)
    assert empty != unreadable
    assert NO_STEPS_LINE in empty
    assert "steps-unavailable" not in empty
    assert STEPS_UNAVAILABLE_LINE in unreadable
    assert "No steps recorded yet" not in unreadable
    assert "<table" not in empty
    assert "<table" not in unreadable


def test_render_run_page_shows_a_table_and_no_message_when_there_are_steps(viewer):
    page = render_run(viewer, run_detail(viewer), [step_summary(viewer)])
    assert '<table id="steps">' in page
    assert page.count("<table") == 1
    assert "no-steps" not in page
    assert "steps-unavailable" not in page


def test_render_run_page_is_a_complete_html_document(viewer):
    page = render_run(viewer, run_detail(viewer), [step_summary(viewer)])
    assert page.startswith("<!doctype html>\n")
    assert page.endswith(DOCUMENT_TAIL)
    assert "Hello from the factory" not in page
    assert "<h1>Runs</h1>" not in page


def test_render_run_page_takes_one_run_page_data_and_no_other_argument(viewer):
    data = viewer.RunPageData(7, run_detail(viewer), (), ENDED_DT)
    assert NO_STEPS_LINE in viewer.render_run_page(data)
    with pytest.raises(TypeError):
        viewer.render_run_page(data, ENDED_DT)
    with pytest.raises(TypeError):
        viewer.render_run_page(data, now=ENDED_DT)


def test_render_run_page_called_twice_with_the_same_data_returns_the_same_page(viewer):
    data = viewer.RunPageData(
        7, running_detail(viewer), (unfinished_step(viewer),), ENDED_DT
    )
    first = viewer.render_run_page(data)
    assert first == viewer.render_run_page(data)
    assert 'id="step-12"' in first


# ------------------------------------------------- render_run_page: the run's facts


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "needs-me"])
def test_render_run_page_labels_the_status_outcome_for_an_ended_run(viewer, outcome):
    page = render_run(viewer, run_detail(viewer, outcome=outcome))
    assert f'<dt>Outcome</dt><dd id="run-status">{outcome}</dd>\n' in page
    assert "<dt>State</dt>" not in page
    assert '<dd id="run-status">ended</dd>' not in page


@pytest.mark.parametrize("state", ["planning", "awaiting_approval", "working"])
def test_render_run_page_labels_the_status_state_for_a_run_that_has_not_ended(
    viewer, state
):
    page = render_run(viewer, running_detail(viewer, state=state))
    assert f'<dt>State</dt><dd id="run-status">{state}</dd>\n' in page
    assert "<dt>Outcome</dt>" not in page


def test_render_run_page_decides_outcome_or_state_by_the_outcome_not_the_state(viewer):
    stale_state = render_run(viewer, run_detail(viewer, state="working", outcome="rejected"))
    assert '<dt>Outcome</dt><dd id="run-status">rejected</dd>\n' in stale_state

    no_outcome = render_run(viewer, run_detail(viewer, state="ended", outcome=""))
    assert '<dt>State</dt><dd id="run-status">ended</dd>\n' in no_outcome


def test_render_run_page_shows_the_status_exactly_once(viewer):
    page = render_run(viewer, run_detail(viewer))
    assert page.count('id="run-status"') == 1


def test_render_run_page_shows_the_reason_of_an_ended_run(viewer):
    page = render_run(viewer, run_detail(viewer, outcome="rejected", reason="tests red"))
    assert (
        '<dt>Outcome</dt><dd id="run-status">rejected</dd>\n'
        '<dt>Reason</dt><dd id="run-reason">tests red</dd>\n'
        '<dt>Attempt limit</dt>'
    ) in page
    assert page.count('id="run-reason"') == 1


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


def test_render_run_page_leaves_out_the_reason_of_a_running_run_without_one(viewer):
    page = render_run(viewer, running_detail(viewer))
    assert run_facts(label="State", status="working", reason=None) in page
    assert "<dt>Reason</dt>" not in page


def test_render_run_page_shows_none_for_an_empty_branch(viewer):
    page = render_run(viewer, run_detail(viewer, branch=""))
    assert '<dt>Branch</dt><dd id="run-branch">none</dd>\n' in page


def test_render_run_page_shows_a_branch_that_is_set(viewer):
    page = render_run(viewer, run_detail(viewer, branch="factory/8-add-a-page-at"))
    assert '<dt>Branch</dt><dd id="run-branch">factory/8-add-a-page-at</dd>\n' in page
    assert ">none<" not in page


def test_render_run_page_shows_the_target(viewer):
    page = render_run(viewer, run_detail(viewer, target="/home/op/code/viewer"))
    assert '<dt>Target</dt><dd id="run-target">/home/op/code/viewer</dd>\n' in page


def test_render_run_page_shows_the_attempt_limit(viewer):
    page = render_run(viewer, run_detail(viewer, attempt_limit=5))
    assert '<dt>Attempt limit</dt><dd id="run-attempt-limit">5</dd>\n' in page


def test_render_run_page_shows_a_long_request_in_full_without_an_ellipsis(viewer):
    request = TEN_WORDS + " eleven twelve thirteen"
    page = render_run(viewer, run_detail(viewer, request=request))
    assert f'<dt>Request</dt><dd id="run-request">{request}</dd>\n' in page
    assert "…" not in page


def test_render_run_page_shows_a_very_long_request_in_full(viewer):
    request = TEN_WORDS + " tail" * 5000
    page = render_run(viewer, run_detail(viewer, request=request))
    assert f'<dd id="run-request">{request}</dd>' in page
    assert page.count("tail") == 5000


def test_render_run_page_keeps_non_ascii_text(viewer):
    page = render_run(
        viewer, run_detail(viewer, request="Ajouter un café ✓", reason="terminé ✓")
    )
    assert '<dd id="run-request">Ajouter un café ✓</dd>' in page
    assert '<dd id="run-reason">terminé ✓</dd>' in page


def test_render_run_page_shows_the_run_facts_when_the_steps_are_unreadable(viewer):
    page = render_run(viewer, run_detail(viewer), viewer.UNREADABLE)
    assert run_facts() in page
    assert "run-unavailable" not in page


def test_render_run_page_run_facts_do_not_depend_on_read_at(viewer):
    early = render_run(viewer, run_detail(viewer), [step_summary(viewer)])
    late = render_run(
        viewer,
        run_detail(viewer),
        [step_summary(viewer)],
        read_at=ENDED_DT + timedelta(days=30),
    )
    assert early == late
    assert "so far" not in early


# ----------------------------------------------------- render_run_page: the steps


def test_render_run_page_shows_the_exact_row_of_a_step_that_passed(viewer):
    step = step_summary(viewer, step_id=3, name="plan", persona="planner", attempt=1)
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_row(3, "plan", "planner", 1, "3m 7s", "passed", "") in page


def test_render_run_page_shows_the_exact_row_of_a_step_that_failed(viewer):
    step = step_summary(viewer, step_id=4, name="build", persona="builder", attempt=3, passed=False)
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_row(4, "build", "builder", 3, "3m 7s", "failed", "") in page


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


def test_render_run_page_says_passed_failed_and_not_finished_in_the_result_cell(viewer):
    steps = [
        step_summary(viewer, step_id=1, passed=True),
        step_summary(viewer, step_id=2, passed=False),
        unfinished_step(viewer, step_id=3),
    ]
    page = render_run(viewer, running_detail(viewer), steps)
    assert re.findall(r'<td class="step-result">([^<]*)</td>', page) == [
        "passed",
        "failed",
        "not finished",
    ]


def test_render_run_page_keeps_step_rows_in_the_order_given(viewer):
    steps = [step_summary(viewer, step_id=n) for n in (30, 4, 12)]
    page = render_run(viewer, run_detail(viewer), steps)
    assert re.findall(r'<tr id="step-(\d+)">', page) == ["30", "4", "12"]


def test_render_run_page_shows_one_row_per_step_inside_one_table(viewer):
    steps = [step_summary(viewer, step_id=n) for n in (1, 2)] + [
        unfinished_step(viewer, step_id=3)
    ]
    page = render_run(viewer, running_detail(viewer), steps)
    assert page.count("<table") == 1
    assert page.count('<tr id="step-') == 3
    assert page.count("so far") == 1
    assert page.count("not finished") == 1
    assert STEPS_TABLE_OPEN + step_row(1) in page
    assert page.endswith(
        step_row(3, duration="3m 7s so far", result="not finished")
        + STEPS_TABLE_CLOSE
        + DOCUMENT_TAIL
    )


def test_render_run_page_shows_every_step_when_there_are_many(viewer):
    steps = [step_summary(viewer, step_id=n) for n in range(1, 301)]
    page = render_run(viewer, run_detail(viewer), steps)
    assert page.count('<tr id="step-') == 300
    assert '<tr id="step-1">' in page
    assert '<tr id="step-300">' in page


def test_render_run_page_shows_every_attempt_of_a_step_as_its_own_row(viewer):
    steps = [
        step_summary(viewer, step_id=1, attempt=1, passed=False),
        step_summary(viewer, step_id=2, attempt=2, passed=True),
    ]
    page = render_run(viewer, run_detail(viewer), steps)
    assert step_row(1, attempt=1, result="failed") in page
    assert step_row(2, attempt=2, result="passed") in page


def test_render_run_page_shows_an_empty_cell_for_an_empty_persona(viewer):
    page = render_run(viewer, run_detail(viewer), [step_summary(viewer, persona="")])
    assert step_row(12, persona="") in page
    assert '<td class="step-persona"></td>' in page


# ------------------------------------------- render_run_page: a step's duration


def test_render_run_page_finished_step_duration_does_not_say_so_far(viewer):
    page = render_run(viewer, running_detail(viewer), [step_summary(viewer)])
    assert step_duration_cell("3m 7s") in page
    assert "so far" not in page


def test_render_run_page_unfinished_step_duration_is_measured_to_read_at(viewer):
    steps = [unfinished_step(viewer)]
    run = running_detail(viewer)
    early = render_run(viewer, run, steps, read_at=CREATED_DT + timedelta(seconds=5))
    late = render_run(
        viewer, run, steps, read_at=CREATED_DT + timedelta(hours=2, seconds=7)
    )
    assert step_duration_cell("5s so far") in early
    assert step_duration_cell("2h 0m 7s so far") in late


def test_render_run_page_uses_read_at_and_not_the_current_time(viewer):
    long_ago = datetime(2001, 1, 1, tzinfo=timezone.utc)
    step = unfinished_step(viewer, started_at=long_ago)
    page = render_run(
        viewer,
        running_detail(viewer),
        [step],
        read_at=long_ago + timedelta(seconds=42),
    )
    assert step_duration_cell("42s so far") in page


@pytest.mark.parametrize("seconds, text", DURATIONS)
def test_render_run_page_formats_the_duration_of_a_finished_step_lasting(
    viewer, seconds, text
):
    step = step_summary(viewer, ended_at=CREATED_DT + timedelta(seconds=seconds))
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_duration_cell(text) in page


@pytest.mark.parametrize("seconds, text", DURATIONS)
def test_render_run_page_formats_the_duration_so_far_of_a_step_running_for(
    viewer, seconds, text
):
    page = render_run(
        viewer,
        running_detail(viewer),
        [unfinished_step(viewer)],
        read_at=CREATED_DT + timedelta(seconds=seconds),
    )
    assert step_duration_cell(text + " so far") in page


def test_render_run_page_shows_a_step_duration_in_whole_seconds(viewer):
    step = step_summary(
        viewer,
        started_at=CREATED_DT.replace(microsecond=123000),
        ended_at=ENDED_DT.replace(microsecond=623000),
    )
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_duration_cell("3m 7s") in page


def test_render_run_page_shows_unknown_duration_for_a_finished_step_without_started_at(
    viewer,
):
    page = render_run(
        viewer, run_detail(viewer), [step_summary(viewer, started_at=None)]
    )
    assert step_row(12, duration="unknown") in page
    assert "so far" not in page


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


def test_render_run_page_shows_zero_seconds_for_a_step_that_ended_before_it_began(
    viewer,
):
    step = step_summary(viewer, ended_at=CREATED_DT - timedelta(minutes=5))
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_duration_cell("0s") in page
    assert 'step-duration">-' not in page


def test_render_run_page_shows_zero_seconds_so_far_when_read_before_the_step_began(
    viewer,
):
    page = render_run(
        viewer,
        running_detail(viewer),
        [unfinished_step(viewer)],
        read_at=CREATED_DT - timedelta(minutes=5),
    )
    assert step_duration_cell("0s so far") in page


# ---------------------------------------------- render_run_page: a step's checks


def test_render_run_page_shows_an_empty_cell_for_a_step_with_no_checks(viewer):
    page = render_run(viewer, run_detail(viewer), [step_summary(viewer, checks=())])
    assert step_checks_cell("") in page
    assert 'class="check' not in page


def test_render_run_page_shows_a_passed_check_by_name_without_a_mark(viewer):
    step = step_summary(viewer, checks=(viewer.Check("guard", True),))
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_checks_cell('<span class="check">guard</span>') in page
    assert "check-failed" not in page
    assert "(failed)" not in page


def test_render_run_page_marks_a_failed_check_with_a_class_and_the_word_failed(viewer):
    step = step_summary(viewer, passed=False, checks=(viewer.Check("tests", False),))
    page = render_run(viewer, run_detail(viewer), [step])
    assert (
        step_checks_cell('<span class="check check-failed">tests (failed)</span>')
        in page
    )
    assert '<span class="check">tests</span>' not in page


def test_render_run_page_marks_only_the_checks_that_failed(viewer):
    check = viewer.Check
    step = step_summary(
        viewer,
        passed=False,
        checks=(
            check("guard", True),
            check("tests", False),
            check("lint", True),
            check("docs", False),
        ),
    )
    page = render_run(viewer, run_detail(viewer), [step])
    assert (
        step_checks_cell(
            check_span("guard")
            + ", "
            + failed_check_span("tests")
            + ", "
            + check_span("lint")
            + ", "
            + failed_check_span("docs")
        )
        in page
    )
    assert page.count("check-failed") == 2
    assert page.count("(failed)") == 2


def test_render_run_page_keeps_checks_in_the_order_given(viewer):
    names = ["zeta", "alpha", "mid"]
    step = step_summary(viewer, checks=tuple(viewer.Check(name, True) for name in names))
    page = render_run(viewer, run_detail(viewer), [step])
    assert re.findall(r'<span class="check">([^<]*)</span>', page) == names


def test_render_run_page_shows_a_single_check_without_a_separator(viewer):
    step = step_summary(viewer, checks=(viewer.Check("guard", True),))
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_checks_cell(check_span("guard")) in page
    assert "</span>," not in page


def test_render_run_page_marks_a_failed_check_of_a_step_that_passed(viewer):
    # The mark follows the check, not the step's own result.
    step = step_summary(viewer, passed=True, checks=(viewer.Check("lint", False),))
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_row(12, result="passed", checks=failed_check_span("lint")) in page


def test_render_run_page_does_not_mark_a_passed_check_of_a_step_that_failed(viewer):
    step = step_summary(viewer, passed=False, checks=(viewer.Check("guard", True),))
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_row(12, result="failed", checks=check_span("guard")) in page
    assert "check-failed" not in page


def test_render_run_page_says_unreadable_for_checks_that_could_not_be_read(viewer):
    step = step_summary(viewer, passed=False, checks=viewer.UNREADABLE)
    page = render_run(viewer, run_detail(viewer), [step])
    assert step_row(12, result="failed", checks="unreadable") in page
    assert 'class="check' not in page


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


def test_render_run_page_escapes_markup_in_the_request(viewer):
    page = render_run(
        viewer, run_detail(viewer, request="<script>alert(1)</script> & more")
    )
    assert (
        '<dd id="run-request">&lt;script&gt;alert(1)&lt;/script&gt; &amp; more</dd>'
        in page
    )
    assert "<script>" not in page


def test_render_run_page_escapes_quotes_in_the_request(viewer):
    request = "say \"hi\" to 'them'"
    page = render_run(viewer, run_detail(viewer, request=request))
    assert f'<dd id="run-request">{html.escape(request)}</dd>' in page
    assert 'say "hi"' not in page


def test_render_run_page_escapes_markup_in_the_target(viewer):
    page = render_run(viewer, run_detail(viewer, target="/tmp/a&b <i>c</i>"))
    assert '<dd id="run-target">/tmp/a&amp;b &lt;i&gt;c&lt;/i&gt;</dd>' in page
    assert "<i>" not in page
    assert "a&b" not in page


def test_render_run_page_escapes_markup_in_the_branch(viewer):
    page = render_run(viewer, run_detail(viewer, branch='factory/7-<b>"x"&y'))
    assert (
        '<dd id="run-branch">' + html.escape('factory/7-<b>"x"&y') + "</dd>" in page
    )
    assert "<b>" not in page


def test_render_run_page_escapes_markup_in_the_outcome(viewer):
    page = render_run(viewer, run_detail(viewer, outcome="<b>&"))
    assert '<dt>Outcome</dt><dd id="run-status">&lt;b&gt;&amp;</dd>' in page
    assert "<b>" not in page


def test_render_run_page_escapes_markup_in_the_state(viewer):
    state = '<i>"working"</i>'
    page = render_run(viewer, running_detail(viewer, state=state))
    assert '<dt>State</dt><dd id="run-status">' + html.escape(state) + "</dd>" in page
    assert "<i>" not in page


def test_render_run_page_escapes_markup_in_the_reason(viewer):
    page = render_run(
        viewer, run_detail(viewer, reason="<script>alert(1)</script> & 'more'")
    )
    assert (
        '<dd id="run-reason">'
        + html.escape("<script>alert(1)</script> & 'more'")
        + "</dd>"
        in page
    )
    assert "<script>" not in page


def test_render_run_page_escapes_markup_in_a_step_name(viewer):
    step = step_summary(viewer, name='<b>te&st</b> "x"')
    page = render_run(viewer, run_detail(viewer), [step])
    assert (
        '<td class="step-name">' + html.escape('<b>te&st</b> "x"') + "</td>" in page
    )
    assert "<b>" not in page


def test_render_run_page_escapes_markup_in_a_step_persona(viewer):
    step = step_summary(viewer, persona="<i>tester</i> & co")
    page = render_run(viewer, run_detail(viewer), [step])
    assert '<td class="step-persona">&lt;i&gt;tester&lt;/i&gt; &amp; co</td>' in page
    assert "<i>" not in page


def test_render_run_page_escapes_markup_in_a_passed_check_name(viewer):
    step = step_summary(viewer, checks=(viewer.Check("<b>guard</b> & 'co'", True),))
    page = render_run(viewer, run_detail(viewer), [step])
    assert check_span(html.escape("<b>guard</b> & 'co'")) in page
    assert "<b>" not in page


def test_render_run_page_escapes_markup_in_a_failed_check_name(viewer):
    step = step_summary(
        viewer, passed=False, checks=(viewer.Check('</span><script>x</script>"', False),)
    )
    page = render_run(viewer, run_detail(viewer), [step])
    assert failed_check_span(html.escape('</span><script>x</script>"')) in page
    assert "<script>" not in page
    assert page.count("</span>") == 1


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
    make_run_db(
        home, runs=[{"request": "from the database"}], steps=[{"name": "stored-step"}]
    )

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
    assert "from the database" not in page
    assert "stored-step" not in page

    assert NO_STEPS_LINE in render_run(viewer, run_detail(viewer), [])
    assert STEPS_UNAVAILABLE_LINE in render_run(
        viewer, run_detail(viewer), viewer.UNREADABLE
    )
    assert RUN_UNAVAILABLE_LINE in render_run(
        viewer, viewer.UNREADABLE, viewer.UNREADABLE
    )
    assert run_not_found_line(7) in render_run(viewer, None, ())


def test_render_run_page_told_the_run_was_not_found_ignores_the_run_in_the_database(
    viewer, home
):
    make_run_db(home, runs=[{"request": "from the database"}], steps=[{}])
    page = render_run(viewer, None, ())
    assert run_not_found_line(7) in page
    assert "from the database" not in page
    assert "<table" not in page


def test_render_run_page_with_no_steps_ignores_steps_in_the_database(viewer, home):
    make_run_db(home, runs=[{}], steps=[{"name": "stored-step"}])
    page = render_run(viewer, run_detail(viewer), [])
    assert NO_STEPS_LINE in page
    assert "stored-step" not in page


# --------------------------------------- render_runs_page: the link to each run


def test_render_runs_page_links_each_id_to_the_page_of_its_run(viewer):
    runs = [ended_summary(viewer, run_id=n) for n in (12, 9, 5)]
    page = render_runs(viewer, runs)
    assert re.findall(
        r'<tr id="run-(\d+)"><td class="run-id"><a href="([^"]*)">([^<]*)</a></td>',
        page,
    ) == [("12", "/runs/12", "12"), ("9", "/runs/9", "9"), ("5", "/runs/5", "5")]
    assert page.count("<a ") == 3


def test_render_runs_page_links_a_running_run_to_its_page(viewer):
    page = render_runs(viewer, [running_summary(viewer, run_id=7)])
    assert '<td class="run-id"><a href="/runs/7">7</a></td>' in page


def test_render_runs_page_has_no_links_when_there_are_no_runs(viewer):
    assert "<a " not in render_runs(viewer, [])
    assert "href" not in viewer.render_runs_page(
        viewer.RunsPageData(viewer.UNREADABLE, ENDED_DT)
    )
    # The link exists once there is a run to link to.
    assert 'href="/runs/7"' in render_runs(viewer, [ended_summary(viewer)])


# -------------------------------------------------------- HTTP server, /runs/<id>


def test_get_run_page_returns_200_html_in_utf8(home, port):
    make_run_db(home, runs=[{}], steps=[{}])
    status, headers, body = fetch(port, "/runs/7")
    assert status == 200
    assert headers["Content-Type"].replace(" ", "").lower() == "text/html;charset=utf-8"
    assert "<h1>Run 7</h1>" in body.decode("utf-8")


def test_get_run_page_content_length_matches_body(home, port):
    make_run_db(home, runs=[{}], steps=[{}])
    status, headers, body = fetch(port, "/runs/7")
    assert status == 200
    assert headers["Content-Length"] is not None
    assert int(headers["Content-Length"]) == len(body)
    assert len(body) > 0


def test_get_run_page_content_length_matches_body_with_non_ascii_text(home, port):
    make_run_db(home, runs=[{"request": "café ✓", "reason": "terminé ✓"}])
    status, headers, body = fetch(port, "/runs/7")
    assert status == 200
    assert '<dd id="run-request">café ✓</dd>' in body.decode("utf-8")
    assert int(headers["Content-Length"]) == len(body)
    assert len(body) > len(body.decode("utf-8"))


def test_get_run_page_body_is_the_run_page_rendered_from_the_run_page_data(
    viewer, home, port
):
    # The run has ended and every step has finished, so the page does not depend on
    # when it was read.
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {"name": "red", "checks": checks_json(("guard", True))},
            {
                "name": "test",
                "attempt": 2,
                "passed": 0,
                "checks": checks_json(("guard", True), ("tests", False)),
            },
        ],
    )
    status, _, body = fetch(port, "/runs/7")
    assert status == 200
    assert body.decode("utf-8") == viewer.render_run_page(viewer.read_run_page_data(7))
    assert b'<table id="steps">' in body


def test_get_run_page_shows_the_run_and_its_steps(home, port):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {
                "step_id": 11,
                "name": "red",
                "persona": "test-writer",
                "checks": checks_json(("guard", True)),
            },
            {
                "step_id": 12,
                "attempt": 2,
                "passed": 0,
                "checks": checks_json(("guard", True), ("tests", False)),
            },
        ],
    )
    status, _, body = fetch(port, "/runs/7")
    assert status == 200
    assert body.decode("utf-8") == (
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


def test_get_run_page_shows_the_run_named_by_the_id_in_the_path(home, port):
    make_run_db(
        home,
        runs=[
            {"run_id": 5, "request": "the fifth request"},
            {"run_id": 12, "request": "the twelfth request"},
        ],
        steps=[
            {"run_id": 5, "name": "step-of-five"},
            {"run_id": 12, "name": "step-of-twelve"},
        ],
    )
    status, _, body = fetch(port, "/runs/12")
    text = body.decode("utf-8")
    assert status == 200
    assert "<h1>Run 12</h1>" in text
    assert '<dd id="run-request">the twelfth request</dd>' in text
    assert "step-of-twelve" in text
    assert "the fifth request" not in text
    assert "step-of-five" not in text


def test_get_run_page_lists_steps_in_id_order(home, port):
    make_run_db(
        home, runs=[{}], steps=[{"step_id": 30}, {"step_id": 4}, {"step_id": 12}]
    )
    _, _, body = fetch(port, "/runs/7")
    assert re.findall(r'<tr id="step-(\d+)">', body.decode("utf-8")) == [
        "4",
        "12",
        "30",
    ]


def test_get_run_page_shows_a_running_run_with_its_state_and_an_unfinished_step(
    home, port
):
    make_run_db(
        home,
        runs=[{"reason": None, "created_at": "2020-01-01T00:00:00Z", **RUNNING}],
        steps=[
            {"step_id": 1, "name": "plan"},
            {
                "step_id": 2,
                "name": "build",
                "started_at": "2020-01-01T00:00:00Z",
                **UNFINISHED,
            },
        ],
    )
    status, _, body = fetch(port, "/runs/7")
    text = body.decode("utf-8")
    assert status == 200
    assert '<dt>State</dt><dd id="run-status">working</dd>\n' in text
    assert "run-reason" not in text
    assert re.search(
        r'<tr id="step-2"><td class="step-name">build</td>'
        r'<td class="step-persona">tester</td>'
        r'<td class="step-attempt">1</td>'
        r'<td class="step-duration">\d+d \d+h \d+m \d+s so far</td>'
        r'<td class="step-result">not finished</td>'
        r'<td class="step-checks"></td></tr>\n',
        text,
    )
    assert step_row(1, name="plan") in text
    assert text.count("so far") == 1


def test_get_run_page_shows_durations_of_steps_stored_with_milliseconds(home, port):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {
                "started_at": "2026-11-18T21:04:05.123Z",
                "ended_at": "2026-11-18T21:07:12.623Z",
            }
        ],
    )
    status, _, body = fetch(port, "/runs/7")
    text = body.decode("utf-8")
    assert status == 200
    assert step_duration_cell("3m 7s") in text
    assert "unknown" not in text


def test_get_run_page_says_no_steps_recorded_yet_for_a_run_without_steps(home, port):
    make_run_db(home, runs=[{}])
    status, _, body = fetch(port, "/runs/7")
    assert status == 200
    assert body.decode("utf-8") == (
        RUN_7_HEAD + run_facts() + NO_STEPS_LINE + DOCUMENT_TAIL
    )


def test_get_run_page_says_unreadable_for_one_steps_bad_checks_and_shows_the_rest(
    home, port
):
    make_run_db(
        home,
        runs=[{}],
        steps=[
            {"step_id": 1, "checks": checks_json(("guard", True))},
            {"step_id": 2, "checks": "not json"},
        ],
    )
    status, _, body = fetch(port, "/runs/7")
    text = body.decode("utf-8")
    assert status == 200
    assert step_row(1, checks=check_span("guard")) in text
    assert step_row(2, checks="unreadable") in text
    assert "steps-unavailable" not in text


def test_get_run_page_escapes_markup_in_stored_text(home, port):
    make_run_db(
        home,
        runs=[
            {
                "request": "<script>alert(1)</script> & more",
                "target": "/tmp/<i>",
                "branch": "factory/<b>",
                "reason": "<u>why</u>",
            }
        ],
        steps=[
            {
                "name": "<em>test</em>",
                "persona": "<s>tester</s>",
                "checks": checks_json(("<q>guard</q>", False)),
            }
        ],
    )
    status, _, body = fetch(port, "/runs/7")
    text = body.decode("utf-8")
    assert status == 200
    assert (
        '<dd id="run-request">&lt;script&gt;alert(1)&lt;/script&gt; &amp; more</dd>'
        in text
    )
    assert failed_check_span("&lt;q&gt;guard&lt;/q&gt;") in text
    for tag in ("<script>", "<i>", "<b>", "<u>", "<em>", "<s>", "<q>"):
        assert tag not in text


def test_get_run_page_ignores_the_query_string(home, port):
    make_run_db(home, runs=[{}], steps=[{}])
    status, _, body = fetch(port, "/runs/7?x=1")
    assert status == 200
    assert body == fetch(port, "/runs/7")[2]
    assert b"<h1>Run 7</h1>" in body


def test_get_run_page_opens_one_read_only_connection_per_request(
    viewer, home, port, monkeypatch
):
    make_run_db(home, runs=[{}], steps=[{}, {}])
    calls = record_connects(viewer, monkeypatch)

    status, _, body = fetch(port, "/runs/7")

    assert status == 200
    assert body.decode("utf-8").count('<tr id="step-') == 2
    assert_one_read_only_connect(calls)


def test_get_run_page_twice_returns_the_same_page_for_an_ended_run(home, port):
    make_run_db(home, runs=[{}], steps=[{}, {"passed": 0}])
    first = fetch(port, "/runs/7")
    second = fetch(port, "/runs/7")
    assert first[0] == second[0] == 200
    assert first[2] == second[2]
    assert b'<table id="steps">' in first[2]


def test_get_run_page_shows_a_step_started_between_two_requests(home, port):
    path = make_run_db(home, runs=[{"reason": None, **RUNNING}], steps=[{}])
    _, _, first = fetch(port, "/runs/7")
    assert re.findall(r'<tr id="step-(\d+)">', first.decode("utf-8")) == ["1"]

    insert_step(path, name="build", **UNFINISHED)

    status, _, second = fetch(port, "/runs/7")
    text = second.decode("utf-8")
    assert status == 200
    assert re.findall(r'<tr id="step-(\d+)">', text) == ["1", "2"]
    assert '<td class="step-result">not finished</td>' in text


def test_get_run_page_shows_the_result_once_an_unfinished_step_has_finished(
    home, port
):
    path = make_run_db(
        home, runs=[{"reason": None, **RUNNING}], steps=[{"step_id": 12, **UNFINISHED}]
    )
    _, _, first = fetch(port, "/runs/7")
    assert '<td class="step-result">not finished</td>' in first.decode("utf-8")

    execute_sql(
        path,
        "UPDATE steps SET passed = 0, ended_at = ?, checks = ? WHERE id = 12",
        (ENDED, checks_json(("tests", False))),
    )

    _, _, second = fetch(port, "/runs/7")
    text = second.decode("utf-8")
    assert (
        step_row(12, result="failed", checks=failed_check_span("tests")) in text
    )
    assert "so far" not in text
    assert "not finished" not in text


def test_get_run_page_shows_the_outcome_and_reason_once_the_run_has_ended(home, port):
    path = make_run_db(home, runs=[{"reason": None, **RUNNING}])
    _, _, first = fetch(port, "/runs/7")
    assert '<dt>State</dt><dd id="run-status">working</dd>' in first.decode("utf-8")

    execute_sql(
        path,
        "UPDATE runs SET state = 'ended', outcome = 'needs-me', "
        "reason = 'interrupted', ended_at = ?",
        (ENDED,),
    )

    _, _, second = fetch(port, "/runs/7")
    text = second.decode("utf-8")
    assert run_facts(status="needs-me", reason="interrupted") in text
    assert "<dt>State</dt>" not in text


def test_get_run_page_leaves_the_database_file_unchanged(home, port):
    path = make_run_db(home, runs=[{}], steps=[{}, {"checks": "not json"}])
    before = path.read_bytes()
    status, _, body = fetch(port, "/runs/7")
    assert status == 200
    assert body.decode("utf-8").count('<tr id="step-') == 2
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_get_run_page_does_not_show_the_home_page_or_the_runs_page(home, port):
    make_run_db(home, runs=[{}], steps=[{}])
    status, _, body = fetch(port, "/runs/7")
    text = body.decode("utf-8")
    assert status == 200
    assert "<title>Run 7 - tomfactory viewer</title>" in text
    assert "Hello from the factory" not in text
    assert "<h1>Runs</h1>" not in text
    assert '<table id="runs">' not in text


# ------------------------------------------- HTTP server, /runs/<id>: an unknown id


def test_get_run_page_for_an_unknown_id_returns_404_with_the_run_not_found_page(
    viewer, home, port
):
    make_run_db(home, runs=[{}], steps=[{}])
    status, headers, body = fetch(port, "/runs/99")
    text = body.decode("utf-8")
    assert status == 404
    assert headers["Content-Type"].replace(" ", "").lower() == "text/html;charset=utf-8"
    assert 'id="run-not-found"' in text
    assert text == RUN_NOT_FOUND_HEAD + run_not_found_line(99) + DOCUMENT_TAIL
    assert text == viewer.render_run_page(viewer.RunPageData(99, None, (), ENDED_DT))


def test_get_run_page_for_an_unknown_id_content_length_matches_body(home, port):
    make_run_db(home, runs=[{}])
    status, headers, body = fetch(port, "/runs/99")
    assert status == 404
    assert b'id="run-not-found"' in body
    assert headers["Content-Length"] is not None
    assert int(headers["Content-Length"]) == len(body)


def test_get_run_page_for_an_unknown_id_does_not_show_another_run(home, port):
    make_run_db(home, runs=[{"request": "the only request"}], steps=[{"name": "only"}])
    status, _, body = fetch(port, "/runs/99")
    text = body.decode("utf-8")
    assert status == 404
    assert run_not_found_line(99) in text
    assert "the only request" not in text
    assert "<table" not in text


def test_get_run_page_returns_404_for_any_id_when_the_runs_table_is_empty(home, port):
    make_run_db(home)
    status, _, body = fetch(port, "/runs/1")
    assert status == 404
    assert run_not_found_line(1) in body.decode("utf-8")


def test_get_run_page_for_the_largest_accepted_id_returns_the_run_not_found_page(
    home, port
):
    make_run_db(home, runs=[{}])
    status, _, body = fetch(port, "/runs/999999999999999999")
    assert status == 404
    assert run_not_found_line(999999999999999999) in body.decode("utf-8")


def test_get_run_page_for_an_unknown_id_ignores_the_query_string(home, port):
    make_run_db(home, runs=[{}])
    status, _, body = fetch(port, "/runs/99?x=1")
    assert status == 404
    assert run_not_found_line(99) in body.decode("utf-8")


def test_get_run_page_for_an_unknown_id_opens_one_read_only_connection(
    viewer, home, port, monkeypatch
):
    make_run_db(home, runs=[{}])
    calls = record_connects(viewer, monkeypatch)

    status, _, body = fetch(port, "/runs/99")

    assert status == 404
    assert b'id="run-not-found"' in body
    assert_one_read_only_connect(calls)


def test_get_run_page_returns_200_once_the_unknown_run_has_been_inserted(home, port):
    path = make_run_db(home)
    status, _, before = fetch(port, "/runs/7")
    assert status == 404
    assert run_not_found_line(7) in before.decode("utf-8")

    insert_detail_run(path)

    status, _, after = fetch(port, "/runs/7")
    assert status == 200
    assert run_facts() in after.decode("utf-8")


def test_server_still_serves_every_page_after_a_run_not_found(home, port):
    make_run_db(home, runs=[{}])
    status, _, body = fetch(port, "/runs/99")
    assert status == 404
    assert b'id="run-not-found"' in body
    assert fetch(port, "/runs/7")[0] == 200
    assert fetch(port, "/runs")[0] == 200
    assert fetch(port, "/")[0] == 200


# --------------------------------------- HTTP server, /runs/<id>: paths that are not ids

NOT_RUN_PAGE_PATHS = [
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


def test_get_run_page_serves_a_run_whose_id_has_several_digits(home, port):
    make_run_db(home, runs=[{"run_id": 1070}])
    status, _, body = fetch(port, "/runs/1070")
    assert status == 200
    assert "<h1>Run 1070</h1>" in body.decode("utf-8")


# ------------------------------------- HTTP server, /runs/<id>: unreadable database


def test_get_run_page_without_database_returns_200_with_run_unavailable(home, port):
    status, headers, body = fetch(port, "/runs/7")
    text = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert text == RUN_7_HEAD + RUN_UNAVAILABLE_LINE + DOCUMENT_TAIL
    assert int(headers["Content-Length"]) == len(body)


def test_get_run_page_without_database_does_not_create_it(home, port):
    status, _, body = fetch(port, "/runs/7")
    assert status == 200
    assert RUN_UNAVAILABLE_LINE in body.decode("utf-8")
    assert not (home / "factory.db").exists()
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("break_database", UNREADABLE_RUN_DATABASES)
def test_get_run_page_returns_200_with_run_unavailable_when(
    home, port, break_database
):
    break_database(home)
    status, _, body = fetch(port, "/runs/7")
    text = body.decode("utf-8")
    assert status == 200
    assert RUN_UNAVAILABLE_LINE in text
    assert "run-not-found" not in text
    assert "<table" not in text
    assert 'id="run"' not in text


@pytest.mark.parametrize("break_steps", UNREADABLE_STEPS_DATABASES)
def test_get_run_page_returns_200_with_the_run_and_steps_unavailable_when(
    home, port, break_steps
):
    break_steps(home)
    status, _, body = fetch(port, "/runs/7")
    assert status == 200
    assert body.decode("utf-8") == (
        RUN_7_HEAD + run_facts() + STEPS_UNAVAILABLE_LINE + DOCUMENT_TAIL
    )


def test_get_run_page_for_an_unknown_id_returns_404_when_the_steps_table_is_missing(
    home, port
):
    _make_run_without_steps_table(home)
    status, _, body = fetch(port, "/runs/99")
    text = body.decode("utf-8")
    assert status == 404
    assert run_not_found_line(99) in text
    assert "unavailable" not in text


def test_get_run_page_recovers_once_the_database_appears(home, port):
    _, _, before = fetch(port, "/runs/7")
    assert RUN_UNAVAILABLE_LINE in before.decode("utf-8")

    make_run_db(home, runs=[{}], steps=[{}])

    status, _, after = fetch(port, "/runs/7")
    text = after.decode("utf-8")
    assert status == 200
    assert run_facts() in text
    assert step_row(1) in text
    assert "unavailable" not in text


# --------------------------------------- HTTP server, the links from /runs to each run


def test_get_runs_links_every_run_to_its_page(home, port):
    make_run_db(home, runs=[{"run_id": 5}, {"run_id": 12}, {"run_id": 9, **RUNNING}])
    status, _, body = fetch(port, "/runs")
    text = body.decode("utf-8")
    assert status == 200
    assert re.findall(
        r'<tr id="run-(\d+)"><td class="run-id"><a href="([^"]*)">([^<]*)</a></td>',
        text,
    ) == [("12", "/runs/12", "12"), ("9", "/runs/9", "9"), ("5", "/runs/5", "5")]
    assert text.count('<tr id="run-') == 3
    assert text.count("<a ") == 3


def test_following_the_link_of_every_run_on_the_runs_page_answers_200(home, port):
    make_run_db(
        home,
        runs=[{"run_id": 5}, {"run_id": 12}, {"run_id": 9, **RUNNING}],
        steps=[{"run_id": 12}],
    )
    _, _, runs_body = fetch(port, "/runs")
    links = re.findall(
        r'<td class="run-id"><a href="([^"]+)">(\d+)</a></td>',
        runs_body.decode("utf-8"),
    )
    assert [run_id for _, run_id in links] == ["12", "9", "5"]
    for href, run_id in links:
        status, _, body = fetch(port, href)
        assert status == 200
        assert f"<h1>Run {run_id}</h1>" in body.decode("utf-8")


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
