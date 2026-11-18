# Changelog

## Unreleased

### Added

- `viewer.py`: a hello-world observability page. It says hello and shows the number of rows in the `runs` table of `factory.db`, found in `$TOMFACTORY_HOME` (default `~/.tomfactory`). The database is opened read-only and the app uses only the Python standard library. Start it with `python viewer.py` and open <http://127.0.0.1:8000/>. See [docs/viewer.md](docs/viewer.md).
- `tests/test_viewer.py`: tests for the viewer.
- `viewer.py`: the page now shows the outcome and reason of the most recent run, which is the row in `runs` with the highest `id`. If that run's `outcome` is empty (`NULL` or `""`), the page says "Latest run has not ended yet" instead. To see it, run `python viewer.py` and open <http://127.0.0.1:8000/>. See "Latest run" in [docs/viewer.md](docs/viewer.md).
- `docs/backlog.md`: findings from the architecture review of this change. The two that were warnings (the page could not tell an empty table from unreadable columns, and the count and latest run were read on separate connections) are fixed and removed. Five suggestions remain: three from that review and two from the review of the fix.

### Changed

- `viewer.py`: the page now says "Latest run unavailable: could not read outcome and reason from the runs table" when the latest run could not be read, and stays silent only when the `runs` table has no rows. The count and the latest run are read on one read-only connection in one read transaction, so a page shows one moment. `count_runs()` and `latest_run()` are replaced by `read_page_data()`, and `render_page` takes one `PageData` value. To see the new line, follow "To see the unavailable line" in [docs/viewer.md](docs/viewer.md); to check everything, run `python -m pytest -q -p no:cacheprovider`.
- Things to know about this change:
  - When the database or the `runs` table is missing, the page now shows two lines, "Run count unavailable" and then "Latest run unavailable". Before, it showed only the first.
  - The "Latest run unavailable" line always gives the same cause, "could not read outcome and reason from the runs table", even when the real cause is a missing file or a locked database. The data layer records that a fact could not be read, not why. This is in `docs/backlog.md`.
  - Code that calls `count_runs()`, `latest_run()` or `render_page(count, latest)` must change: the first two are gone, not kept as wrappers.

### Beyond the request

- A `--port` option (default 8000), so the app can start when port 8000 is taken.
- When the database or the `runs` table is missing, the page still loads and shows "Run count unavailable" in place of the count.
- The server listens on `127.0.0.1` only.
- When the `runs` table has no rows, the page adds nothing below the count. A `runs` table without `outcome` or `reason` columns shows "Latest run unavailable" instead, so a renamed column no longer makes the latest-run lines disappear without a message.
- An ended run with an empty `reason` shows the outcome line only. A run with an empty `outcome` is shown as not ended even if `reason` has text, and that text is not shown.
- The run's id is not shown.
