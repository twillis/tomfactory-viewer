# Run 6: accepted

*change, started 2026-11-18 19:14 UTC, took 9m 33s, an estimated $7.16 of agent time.*

## The request

Fix the two entries marked Warning in docs/backlog.md: make the data layer distinguish 'could not read the database' from 'there are no runs' so the page can say which, and read everything a page needs over one read-only connection, passing a single page-data value to the rendering code. Remove those two entries from docs/backlog.md once done.

## The approved plan (version 1)

# Plan: one page-data value, read once, that says what could not be read

## What changes, in one paragraph

`viewer.py` gets one explicit interface between its data layer and its presentation layer: `read_page_data() -> PageData` and `render_page(data: PageData) -> str`. `read_page_data` opens one read-only connection, reads the count and the latest run inside one read transaction, and marks each fact it could not read with an explicit `UNREADABLE` value instead of `None`. `None` for the latest run then means only 'the table was read and has no rows'. The page shows a new line when the latest run could not be read.

## Files

### `viewer.py` (change)

**Add (data layer)**

- A typed marker for 'could not read', as a single-member enum so it can be used in annotations and compared with `is`:
  ```python
  class Unreadable(Enum):
      UNREADABLE = 'unreadable'
  UNREADABLE = Unreadable.UNREADABLE
  ```
  (`enum` is standard library; the stdlib-only test still holds.)
- `PageData(NamedTuple)`, everything the page shows:
  - `count: int | Unreadable`. Number of rows in `runs`, or `UNREADABLE`.
  - `latest: LatestRun | None | Unreadable`. The run with the highest id; `None` only when `runs` was read and has no rows; `UNREADABLE` when `outcome`/`reason` could not be read.
  - No defaults: both fields are always given.
- `read_page_data() -> PageData`:
  1. Open exactly one connection: `sqlite3.connect(db_path().resolve().as_uri() + '?mode=ro', uri=True)` (same call as today). If this raises `sqlite3.Error`, return `PageData(UNREADABLE, UNREADABLE)`.
  2. `conn.execute('BEGIN')`, so both reads see the same moment (in WAL mode, which the factory uses, one read transaction is one snapshot).
  3. `SELECT COUNT(*) FROM runs` gives `count = int(row[0])`; on `sqlite3.Error`, `count = UNREADABLE`.
  4. `SELECT outcome, reason FROM runs ORDER BY id DESC LIMIT 1` gives `LatestRun(row[0] or '', row[1] or '')`, or `None` if there is no row; on `sqlite3.Error`, `latest = UNREADABLE`.
  5. Each of steps 2–4 has its own `try/except sqlite3.Error`, so a missing column does not lose the count. If `BEGIN` fails, both are `UNREADABLE`.
  6. Close the connection in a `finally`; never commit. The transaction lasts two short reads, which respects 'readers must not hold long transactions' in `docs/run-store.md`.
  7. Use only `conn.execute(...)` and `conn.close()` on the connection, so a test can wrap it.

**Remove (data layer)**

- `_fetch_one`, `count_runs`, `latest_run`. They are the ambiguous, one-connection-per-fact interface the two warnings describe; keeping them would leave two ways to read. `db_path` and `LatestRun` stay as they are.

**Change (presentation layer)**

- `render_page(data: PageData) -> str` takes one required argument and no others.
  - Count line: `data.count is UNREADABLE` gives today's `run-count-unavailable` paragraph, text unchanged; otherwise today's `run-count` line.
  - `_render_latest(latest)`:
    - `UNREADABLE` gives the new line `<p id="latest-run-unavailable">Latest run unavailable: could not read outcome and reason from the runs table</p>\n`.
    - `None` gives `''` (unchanged: the count line already says 0).
    - `LatestRun` is rendered exactly as today.
  - The two facts render independently. When the database is missing, the page shows both the count-unavailable line and the latest-run-unavailable line, in that order.
- `render_page` still calls `db_path()` for the count-unavailable message. That is a separate Suggestion in the backlog and is not part of this request; leave it.

**Change (HTTP layer)**

- `_Handler.do_GET`: `body = render_page(read_page_data()).encode('utf-8')`. Nothing else changes.
- Update the module docstring only if it names removed functions (it does not today).

### `tests/test_viewer.py` (change, by the test writer)

The existing tests call `count_runs()`, `latest_run()` and `render_page(count, latest)`; those must be rewritten against the new interface. Tests for `db_path`, `main`, 404/501, headers and stdlib-only stay as they are. What the tests should show:

- **`read_page_data` states**
  - Table with ended/not-ended rows gives `PageData(n, LatestRun(...))`, keeping the existing highest-id, NULL-to-`''` and text-unchanged cases.
  - Empty `runs` table with outcome columns gives `PageData(0, None)`.
  - `runs` without `outcome` and/or `reason` (3 rows) gives `PageData(3, UNREADABLE)`.
  - Missing file, missing home directory, empty file, non-SQLite file, `factory.db` as a directory, and no `runs` table each give `PageData(UNREADABLE, UNREADABLE)`.
  - `count` is of type `int` when readable; `latest is None` and `latest is UNREADABLE` are distinct results for the empty and missing-column cases.
- **One read-only connection**: with `viewer.sqlite3.connect` replaced by a recorder, one `read_page_data()` call and one `GET /` each make exactly one `connect` call, with `mode=ro` in the URI and `uri=True`.
- **Never writes or creates**: file bytes and directory listing are unchanged after calls; a missing file or directory is not created.
- **One moment**: on a database created with `PRAGMA journal_mode=WAL` holding one run, wrap the connection returned by `connect` in a proxy that, right after the `COUNT` statement executes, inserts a second run through a separate connection. The result must be `PageData(1, <the first run>)`, not a count of 1 with the second run.
- **`render_page`**
  - `PageData(3, None)` has no `latest-run` text.
  - `PageData(3, UNREADABLE)` contains the exact `latest-run-unavailable` line right after the count line, and no `latest-run-outcome` or `latest-run-not-ended`.
  - `PageData(UNREADABLE, UNREADABLE)` contains both unavailable lines and no `id="run-count"`.
  - Existing ended, not-ended, empty-reason, escaping and ordering cases are restated with `PageData(count, LatestRun(...))`.
  - `render_page` does not open the database (the existing forbidden-connect test).
- **HTTP**
  - The body equals `viewer.render_page(viewer.read_page_data())`.
  - Empty table: count 0 and no `latest-run`.
  - `runs` without outcome columns: count 3 plus the `latest-run-unavailable` line.
  - No database: both unavailable lines, status 200, file not created.
  - Freshness between requests is kept.
- `viewer` no longer has `count_runs`, `latest_run` or `_fetch_one`.

### `docs/backlog.md` (change)

- Delete the two **Warning** lines; keep the three Suggestions.
- In the last Suggestion, replace `render_page(count_runs(), latest_run())` with `render_page(read_page_data())` so it does not name removed functions.

### `docs/viewer.md` (change)

- 'Latest run': replace the 'There is no run to show' bullet with two:
  - no rows: nothing is added, the count shows 0;
  - latest run could not be read (database, table, or `outcome`/`reason` column missing): the page shows 'Latest run unavailable: could not read outcome and reason from the runs table'.
- Delete the paragraph about two separate connections; say instead that the count and latest run are read in one read-only connection and one transaction, so a page shows one moment.
- 'When the count is not available': note that the latest-run-unavailable line appears below it.

### `CHANGELOG.md` (change)

- Under Unreleased, add a 'Changed' entry: the page now says when the latest run could not be read; count and latest run are read on one read-only connection; `count_runs()`/`latest_run()` are replaced by `read_page_data()` and `render_page` takes one `PageData`.
- Correct the 'Beyond the request' bullet that says a renamed column makes the lines disappear without a message.
- Correct the backlog line that says two findings are warnings.

## Assumptions

- 'So the page can say which' is met by showing a message only when unreadable and staying silent for an empty table, where 'Runs recorded: 0' already says there are no runs. No 'no runs yet' line is added.
- Removing `count_runs`/`latest_run` instead of keeping them as wrappers is intended: one interface between the layers, at the cost of rewriting the tests that call them.
- When the whole database is unreadable, showing both unavailable lines is acceptable. It keeps the rendering rules per fact, with no rule that ties one fact to another.

## Check

`python -m pytest -q -p no:cacheprovider` passes.

## How it went

| Step | Attempt | Result |
|------|---------|--------|
| plan | 1 | passed |
| baseline | 1 | passed |
| write_tests | 1 | passed |
| red | 1 | passed |
| build | 1 | passed |
| test | 1 | passed |
| architecture_review | 1 | passed |
| document | 1 | passed |
| review | 1 | passed |
| finish | 1 | passed |

**Outcome: accepted.** the tests pass and the review found the result matches the request, the plan and the ideals

## The review

Verdict: **accept**

- The data layer now marks each fact it could not read as UNREADABLE, and latest is None only when the runs table was read and has no rows, so the page shows 'Latest run unavailable' for an unreadable run and stays silent below 'Runs recorded: 0' for an empty table.
- read_page_data() opens exactly one read-only connection, reads the count and the latest run inside one read transaction, closes it in a finally and never commits; do_GET passes its single PageData value to render_page, which takes no other argument.
- The two Warning entries are removed from docs/backlog.md and the remaining test Suggestion no longer names the removed functions.
- The change follows the approved plan step by step: _fetch_one, count_runs and latest_run are removed rather than kept as wrappers, the tests cover every state the plan listed including the WAL one-moment case, and docs/viewer.md and CHANGELOG.md are updated as planned.
- The one addition beyond the plan is a 'From run 6' backlog section with two new Suggestions from the architect's review of this fix; the CHANGELOG states the new total and I do not count it as a departure.
- The ideal of well-defined layers with explicit interfaces is followed: there is one typed interface between data and presentation. render_page still calling db_path() and the fixed 'could not read outcome and reason' wording when the file is missing are both as the plan specified, are recorded as backlog Suggestions, and are not regressions.
- Not covered by tests, and not a reason to reject: an error that is not a sqlite3.Error (for example an OSError while resolving the path) would still escape read_page_data as it did before, and a WAL database that nothing else has open or that sits in an unwritable directory is untested, which docs/viewer.md states under Limits.
- I confirmed viewer.py and docs/backlog.md in the worktree match the diff; I did not re-run the tests and rely on the factory's result of 158 passed.

## Commits it made on main

- `62a0d2a` factory: red (run 6)
- `6c9191d` factory: test (run 6)
- `e931455` factory: architecture_review (run 6)
- `da6af06` factory: document (run 6)
