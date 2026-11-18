# Changelog

## Unreleased

### Added

- `viewer.py`: a hello-world observability page. It says hello and shows the number of rows in the `runs` table of `factory.db`, found in `$TOMFACTORY_HOME` (default `~/.tomfactory`). The database is opened read-only and the app uses only the Python standard library. Start it with `python viewer.py` and open <http://127.0.0.1:8000/>. See [docs/viewer.md](docs/viewer.md).
- `tests/test_viewer.py`: tests for the viewer.

### Beyond the request

- A `--port` option (default 8000), so the app can start when port 8000 is taken.
- When the database or the `runs` table is missing, the page still loads and shows "Run count unavailable" in place of the count.
- The server listens on `127.0.0.1` only.
