# Changelog

## Unreleased

### Added

- `viewer.py`: a hello-world observability page. It says hello and shows the number of rows in the `runs` table of `factory.db`, found in `$TOMFACTORY_HOME` (default `~/.tomfactory`). The database is opened read-only and the app uses only the Python standard library. Start it with `python viewer.py` and open <http://127.0.0.1:8000/>. See [docs/viewer.md](docs/viewer.md).
- `tests/test_viewer.py`: tests for the viewer.
- `viewer.py`: the page now shows the outcome and reason of the most recent run, which is the row in `runs` with the highest `id`. If that run's `outcome` is empty (`NULL` or `""`), the page says "Latest run has not ended yet" instead. To see it, run `python viewer.py` and open <http://127.0.0.1:8000/>. See "Latest run" in [docs/viewer.md](docs/viewer.md).
- `docs/backlog.md`: findings from the architecture review of this change. The two that were warnings (the page could not tell an empty table from unreadable columns, and the count and latest run were read on separate connections) are fixed and removed. Five suggestions remain: three from that review and two from the review of the fix.
- `viewer.py`: a second page at `/runs` listing every run, highest `id` first. Each row shows the run's id, its outcome once ended or its state until then, the first 10 words of the request, when it was created (UTC), and how long it took or has been running ("so far"). The home page links to it with "All runs", just below the heading. To see it, run `python viewer.py` and open <http://127.0.0.1:8000/runs>. See "All runs" in [docs/viewer.md](docs/viewer.md).
- Things to know about the `/runs` page:
  - A run whose driver died but which is not marked ended shows its stored state and a duration that keeps growing.
  - A timestamp not written exactly as `2026-11-18T21:04:05Z` shows "unknown" for created and for the duration.
  - An empty `runs` table shows "No runs recorded yet". An unreadable database, table or column shows "Runs unavailable: could not read the runs table", with status 200.
  - There is no paging and no link back to the home page. `/runs/` and `/runs/7` return 404.
- `docs/backlog.md`: eight findings from the architecture review of the `/runs` page, one warning and seven suggestions. None is fixed in this change. The warning is that the standard-library-only import test also rejects imports of this codebase's own modules, so the viewer's layers cannot be split into separate files.
- `viewer.py`: a page at `/runs/<id>/steps/<step id>` showing one step in full: its name, persona, attempt, result and duration, what it was asked, what it produced (one entry per key), each check with its result and note, and its events in `id` order with time, kind and summary. To see it, run `python viewer.py`, open the page of a run and follow a step's name. See "One step" in [docs/viewer.md](docs/viewer.md).
- Things to know about the step page:
  - An unknown step, a step that belongs to another run and an unknown run all give the same 404 page, "No step with id … in run …". It does not say which.
  - An unreadable database or `steps` table shows "Step unavailable: could not read the steps table", with status 200. Unreadable `produced`, `checks` or `events` each show their own "unavailable" line and the rest of the page is kept.
  - Every event of the step is on the one page; there is no paging. Event payloads are not shown, and there is no link back to the run.
- `docs/backlog.md`: seven suggestions from the architecture review of the step page. None is fixed in this change.
- `viewer.py`: the run page (`/runs/<id>`), while the run has not ended, now reloads itself every 5 seconds and says which step the run is on and for how long, for example "Current step: test, 3m 7s so far", or "No step is running now". The page of an ended run is unchanged and does not refresh. To see it, run `python viewer.py` and open the page of a run in progress. See "One run while it is running" in [docs/viewer.md](docs/viewer.md).
- Things to know about the live run page:
  - The current step is the run's newest step row, if it has not finished. It is not read from `runs.current_step`.
  - The refresh is a `<meta http-equiv="refresh">` tag; there is no JavaScript.
  - The "run not found" and "could not read the runs table" pages do not refresh. An open page of a running run that hits one unreadable read stops refreshing until reloaded by hand.
  - A run whose driver died but which is not marked ended keeps refreshing, with a step time that keeps growing.
  - The home, runs and step pages still do not refresh.
- `docs/backlog.md`: five findings from the architecture review of the live run page, one warning and four suggestions. None is fixed in this change. The warning is that the current step is taken as the last step in display order, so listing steps newest first would silently name the oldest step as current.

### Changed

- `viewer.py`: on the run page, each step's name in the steps table is now a link to that step's page. Nothing else in the row changed.
- `viewer.py`: `Check` has a third field, `note` (default `""`). `read_run_page_data` gives the same results as before, now through a shared helper, `_read_one_and_its_rows`, that the step page also uses.
- `viewer.py`: the home page has one new line, the "All runs" link, between the heading and the count. Nothing else on it changed.
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
