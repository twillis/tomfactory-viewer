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


def make_db(home, rows=0, with_runs_table=True):
    """Create ``home/factory.db``; optionally with a ``runs`` table holding ``rows`` rows."""
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


# ------------------------------------------------------------------------ count_runs


def test_count_runs_returns_number_of_rows(viewer, home):
    make_db(home, rows=3)
    assert viewer.count_runs() == 3


def test_count_runs_returns_zero_for_empty_runs_table(viewer, home):
    make_db(home, rows=0)
    count = viewer.count_runs()
    assert count == 0
    assert count is not None


def test_count_runs_counts_a_single_row(viewer, home):
    make_db(home, rows=1)
    assert viewer.count_runs() == 1


def test_count_runs_returns_an_int(viewer, home):
    make_db(home, rows=2)
    assert type(viewer.count_runs()) is int


def test_count_runs_sees_rows_added_between_calls(viewer, home):
    path = make_db(home, rows=3)
    assert viewer.count_runs() == 3
    insert_run(path)
    assert viewer.count_runs() == 4


def test_count_runs_returns_none_when_database_file_is_missing(viewer, home):
    assert viewer.count_runs() is None


def test_count_runs_does_not_create_missing_database_file(viewer, home):
    viewer.count_runs()
    assert not (home / "factory.db").exists()
    assert list(home.iterdir()) == []


def test_count_runs_returns_none_and_does_not_create_missing_home_directory(
    viewer, tmp_path, monkeypatch
):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("TOMFACTORY_HOME", str(missing))
    assert viewer.count_runs() is None
    assert not missing.exists()


def test_count_runs_returns_none_when_runs_table_is_missing(viewer, home):
    make_db(home, with_runs_table=False)
    assert viewer.count_runs() is None


def test_count_runs_returns_none_when_database_file_is_empty(viewer, home):
    (home / "factory.db").write_bytes(b"")
    assert viewer.count_runs() is None


def test_count_runs_returns_none_when_file_is_not_a_sqlite_database(viewer, home):
    (home / "factory.db").write_bytes(b"this is definitely not a sqlite database\n" * 50)
    assert viewer.count_runs() is None


def test_count_runs_returns_none_when_factory_db_is_a_directory(viewer, home):
    (home / "factory.db").mkdir()
    assert viewer.count_runs() is None


def test_count_runs_works_when_home_path_contains_a_space(viewer, tmp_path, monkeypatch):
    spaced = tmp_path / "factory home"
    make_db(spaced, rows=2)
    monkeypatch.setenv("TOMFACTORY_HOME", str(spaced))
    assert viewer.count_runs() == 2


def test_count_runs_opens_database_read_only(viewer, home, monkeypatch):
    make_db(home, rows=3)
    calls = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(viewer.sqlite3, "connect", recording_connect)

    assert viewer.count_runs() == 3
    assert len(calls) == 1
    args, kwargs = calls[0]
    database = args[0] if args else kwargs["database"]
    assert "mode=ro" in str(database)
    assert kwargs.get("uri") is True


def test_count_runs_leaves_database_file_unchanged(viewer, home):
    path = make_db(home, rows=3)
    before = path.read_bytes()
    viewer.count_runs()
    viewer.count_runs()
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


def test_count_runs_leaves_database_without_runs_table_unchanged(viewer, home):
    path = make_db(home, with_runs_table=False)
    before = path.read_bytes()
    viewer.count_runs()
    assert path.read_bytes() == before
    assert [p.name for p in home.iterdir()] == ["factory.db"]


# ----------------------------------------------------------------------- render_page


def test_render_page_says_hello(viewer, home):
    assert "<h1>Hello from the factory</h1>" in viewer.render_page(3)


def test_render_page_is_a_complete_html_document_with_title(viewer, home):
    page = viewer.render_page(3)
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert "<title>tomfactory viewer</title>" in page


def test_render_page_shows_the_run_count(viewer, home):
    page = viewer.render_page(3)
    assert '<p>Runs recorded: <strong id="run-count">3</strong></p>' in page
    assert "run-count-unavailable" not in page


def test_render_page_shows_zero_as_a_count_not_as_unavailable(viewer, home):
    page = viewer.render_page(0)
    assert '<strong id="run-count">0</strong>' in page
    assert "run-count-unavailable" not in page


def test_render_page_shows_a_large_count_in_full(viewer, home):
    assert '<strong id="run-count">1234567</strong>' in viewer.render_page(1234567)


def test_render_page_shows_unavailable_message_with_database_path_for_none(viewer, home):
    page = viewer.render_page(None)
    expected = (
        '<p id="run-count-unavailable">Run count unavailable: '
        f"no readable runs table in {html.escape(str(home / 'factory.db'))}</p>"
    )
    assert expected in page
    assert "<h1>Hello from the factory</h1>" in page
    assert 'id="run-count"' not in page


def test_render_page_escapes_html_in_the_database_path(viewer, tmp_path, monkeypatch):
    nasty = tmp_path / "a&b<i>c"
    monkeypatch.setenv("TOMFACTORY_HOME", str(nasty))
    page = viewer.render_page(None)
    assert html.escape(str(nasty / "factory.db")) in page
    assert "<i>" not in page
    assert "a&b" not in page


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


def test_get_root_body_is_the_rendered_page(viewer, home, port):
    make_db(home, rows=3)
    _, _, body = fetch(port)
    assert body.decode("utf-8") == viewer.render_page(3)


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


def test_get_root_without_database_returns_200_with_unavailable_message(home, port):
    status, headers, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "Hello from the factory" in text
    assert 'id="run-count-unavailable"' in text
    assert 'id="run-count"' not in text


def test_get_root_without_database_does_not_create_it(home, port):
    fetch(port)
    assert not (home / "factory.db").exists()


def test_get_root_without_runs_table_returns_200_with_unavailable_message(home, port):
    make_db(home, with_runs_table=False)
    status, _, body = fetch(port)
    text = body.decode("utf-8")
    assert status == 200
    assert "Hello from the factory" in text
    assert 'id="run-count-unavailable"' in text


def test_get_root_recovers_once_database_appears(home, port):
    _, _, before = fetch(port)
    assert 'id="run-count-unavailable"' in before.decode("utf-8")

    make_db(home, rows=2)

    _, _, after = fetch(port)
    assert '<strong id="run-count">2</strong>' in after.decode("utf-8")


def test_get_root_leaves_database_file_unchanged(home, port):
    path = make_db(home, rows=3)
    before = path.read_bytes()
    fetch(port)
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
