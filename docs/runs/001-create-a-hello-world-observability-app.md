# Run 1: accepted

*change, started 2026-11-18 17:09 UTC, took 3m 37s, an estimated $2.06 of agent time.*

## The request

Create a hello-world observability app for the factory: a small web page I can open in a browser that says hello and shows how many runs the factory has recorded. The factory keeps its runs in a SQLite file named factory.db in the directory given by the TOMFACTORY_HOME environment variable (default ~/.tomfactory), in a table named runs with one row per run. Open the database read-only. Use only the Python standard library. Say in the documentation how to start it.

## The approved plan (version 1)

# Plan: hello-world observability page

## Context
The repository holds only `README.md`, `.gitignore` and `.factory.toml`. `.factory.toml` sets the test command (`python -m pytest -q -p no:cacheprovider`, run from the repository root), puts tests in `tests/`, and puts documentation in `docs/` and `CHANGELOG.md`.

## Files

### 1. Add `viewer.py` (repository root; the implementation)
One module that imports only the standard library (`argparse`, `html`, `os`, `sqlite3`, `pathlib`, `http.server`). Public surface, which the tests rely on:

- `db_path() -> pathlib.Path`: returns `<home>/factory.db`, where `<home>` is the `TOMFACTORY_HOME` environment variable, or `~/.tomfactory` (with `~` expanded) if the variable is unset or empty. It reads the environment on every call, not at import.
- `count_runs() -> int | None`: opens `db_path()` read-only with `sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)`, runs `SELECT COUNT(*) FROM runs`, closes the connection and returns the integer. It returns `None` on any `sqlite3.Error` (missing file, missing table, unreadable file). It must never create the file or the directory.
- `render_page(count: int | None) -> str`: returns a complete HTML document (`<!doctype html>`, `<title>tomfactory viewer</title>`) containing `<h1>Hello from the factory</h1>` and then:
  - if `count` is an integer: `<p>Runs recorded: <strong id="run-count">N</strong></p>`
  - if `count` is `None`: `<p id="run-count-unavailable">Run count unavailable: no readable runs table in PATH</p>`, where PATH is `html.escape(str(db_path()))`.
- `make_server(host: str = "127.0.0.1", port: int = 8000) -> http.server.ThreadingHTTPServer`: returns a bound server that has not started serving. Its handler behaves as follows:
  - `GET /` (ignoring any query string) returns 200 with `Content-Type: text/html; charset=utf-8`, a `Content-Length` header, and a body of `render_page(count_runs())` encoded as UTF-8. The count is computed afresh on every request.
  - `GET` on any other path returns 404 via `send_error(404)`.
  - No other methods are implemented.
- `main(argv=None) -> None`: uses `argparse` with a single option `--port` (int, default 8000), calls `make_server(port=args.port)`, prints `Serving on http://127.0.0.1:<port>/`, then `serve_forever()`. It exits cleanly on `KeyboardInterrupt` and closes the server.
- The module ends with `if __name__ == "__main__": main()`.

### 2. Add `tests/test_viewer.py` (the test writer)
Use pytest with `tmp_path` and `monkeypatch`. A helper creates `tmp_path/factory.db` with `CREATE TABLE runs (id INTEGER PRIMARY KEY)` and inserts N rows. A fixture starts `make_server(port=0)` in a daemon thread, yields `server.server_address[1]`, then calls `shutdown()` and `server_close()`. Fetch pages with `urllib.request`.

The tests should show that:
1. `db_path()` equals `tmp_path / "factory.db"` when `TOMFACTORY_HOME` is `tmp_path`.
2. `db_path()` equals `<HOME>/.tomfactory/factory.db` when `TOMFACTORY_HOME` is unset, and also when it is empty (monkeypatch `HOME` to `tmp_path`).
3. `count_runs()` returns 3 with 3 rows and 0 with an empty `runs` table.
4. `count_runs()` returns `None` when the home directory has no `factory.db`, and afterwards `factory.db` still does not exist.
5. `count_runs()` returns `None` when `factory.db` exists but has no `runs` table.
6. The database is opened read-only: patch `viewer.sqlite3.connect` with a wrapper that records its arguments, then assert the database argument contains `mode=ro` and that `uri=True` was passed. Also assert the file's bytes are unchanged after `count_runs()`.
7. `GET /` returns 200, the content type starts with `text/html`, and the body contains `Hello from the factory` and `<strong id="run-count">3</strong>`.
8. After a row is inserted while the server is running, the next `GET /` shows `4`.
9. `GET /` with no database returns 200 and the body contains `Hello from the factory` and `id="run-count-unavailable"`.
10. `GET /nope` returns 404.
11. `viewer.py` uses only the standard library: parse the source with `ast` and assert every imported top-level module name is in `sys.stdlib_module_names`.

### 3. Add `docs/viewer.md` (the documenter)
A short page that says:
- What it is: a page that says hello and shows the number of rows in the `runs` table.
- How to start it: from the repository root run `python viewer.py`, then open `http://127.0.0.1:8000/`. Use `python viewer.py --port 9000` for another port, and Ctrl+C to stop.
- Where it reads from: `factory.db` in `$TOMFACTORY_HOME`, default `~/.tomfactory`. It is opened read-only and never created or changed.
- What it shows when the database or table is missing: the "Run count unavailable" message.
- Requirements: Python 3.10 or later and nothing to install.

## Assumptions
- **Missing database or table.** The request does not say what to do. The plan still serves the hello page (200) with an "unavailable" message instead of failing, because that is more useful for an observability page.
- **Local only.** The server binds to `127.0.0.1`, since the request is for a page the operator opens in their own browser.
- **`--port` is the one addition beyond the request.** It is there so the app can still start when port 8000 is taken, and it gives tests a free port through `make_server(port=0)`. Drop the flag if it is unwanted; `make_server` still needs the parameter.
- **Documentation goes in `docs/viewer.md`, not `README.md`.** `.factory.toml` only lets the documenter write under `docs/` and `CHANGELOG.md`. `README.md` is left unchanged.
- **Single root module.** `viewer.py` at the root, not a package, is the smallest layout, and it is importable in tests because `python -m pytest` puts the repository root on `sys.path`.
- **Python version.** `sys.stdlib_module_names` and the `int | None` annotations need Python 3.10 or later.
- **pytest is test-only.** It is used only by tests, as `.factory.toml` already requires. The app itself has no third-party dependency.

## How it went

| Step | Attempt | Result |
|------|---------|--------|
| plan | 1 | passed |
| baseline | 1 | passed |
| write_tests | 1 | passed |
| red | 1 | passed |
| build | 1 | passed |
| test | 1 | passed |
| document | 1 | passed |
| review | 1 | passed |
| finish | 1 | passed |

**Outcome: accepted.** the tests pass and the review found the result matches the request, the plan and the ideals

## The review

Verdict: **accept**

- The change does what was asked: GET / serves a page that says 'Hello from the factory' and shows the number of rows in the runs table of factory.db, located via TOMFACTORY_HOME with a default of ~/.tomfactory.
- The database is opened read-only through a file: URI with mode=ro and uri=True; tests confirm the file's bytes are unchanged and that a missing file or directory is never created.
- viewer.py imports only standard-library modules (argparse, html, os, sqlite3, http.server, pathlib); pytest is used only by the tests.
- docs/viewer.md says how to start it (python viewer.py, then open http://127.0.0.1:8000/), and CHANGELOG.md repeats the start command.
- The implementation, tests and documentation follow the approved plan with no departures; the additions beyond the request (--port, the 'Run count unavailable' page, binding to 127.0.0.1 only) are the plan's stated assumptions and are listed in the changelog.
- All 51 tests passed in the factory's run; they cover every case the plan listed plus further edge cases.
- Not covered by tests, and worth a manual check if the factory database uses WAL mode: SQLite may create -shm and -wal files beside the database even for a read-only connection. The documentation states this limit.
- Minor and as planned: with --port 0 the startup line prints port 0, not the port actually bound, and README.md does not mention the viewer.
- No operator ideals were stated, so none are broken.

## Commits it made on main

- `9a7ad69` factory: red (run 1)
- `f756972` factory: test (run 1)
- `2c8c666` factory: document (run 1)
