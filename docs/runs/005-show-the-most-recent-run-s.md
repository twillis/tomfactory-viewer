# Run 5: accepted

*change, started 2026-11-18 18:23 UTC, took 5m 17s, an estimated $3.14 of agent time.*

## The request

Show the most recent run's outcome and reason on the page. The runs table has an integer id and text columns named outcome and reason, which are empty until a run has ended.

## The approved plan (version 1)

# Plan: show the most recent run's outcome and reason

## What the codebase is today

`viewer.py` is one stdlib-only file with three layers, each a function: data access (`db_path`, `count_runs`), presentation (`render_page`) and HTTP (`_Handler`, `make_server`, `main`). The handler is the only place that joins them: `render_page(count_runs())`. This plan keeps that shape and adds one explicit value type between data access and presentation.

## Assumptions

- "Most recent run" means the row with the highest `id`.
- "Empty" means SQL `NULL` or the empty string; both are treated the same.
- A run has ended when its `outcome` is non-empty. A row with an empty `outcome` is shown as not ended, even if `reason` has text.
- When there is no run to show (no rows, no database, no `runs` table, or no `outcome`/`reason` columns), the page adds nothing. The existing count line (`Runs recorded: 0` or `Run count unavailable…`) already explains why.
- The run's id is not shown, because the request asks only for outcome and reason.

## Changes to `viewer.py` (the only source file changed)

1. **Interface type.** Add `from typing import NamedTuple` and:
   ```python
   class LatestRun(NamedTuple):
       outcome: str  # "" until the run has ended
       reason: str   # "" until the run has ended
   ```
   This is the only thing the presentation layer knows about a run. No sqlite rows or column names cross the boundary.

2. **Data access.**
   - Add a private helper `_fetch_one(sql: str) -> tuple | None`. It holds the connect/execute/close code now inside `count_runs`: one `sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)` call, `fetchone()`, close in `finally`, and `None` on `sqlite3.Error`.
   - Rewrite `count_runs()` on top of it, with unchanged behaviour: `None` if the row is `None`, else `int(row[0])`.
   - Add `latest_run() -> LatestRun | None`. It runs `SELECT outcome, reason FROM runs ORDER BY id DESC LIMIT 1`. It returns `None` when the helper returns `None` (no rows or unreadable). Otherwise it returns `LatestRun(outcome or "", reason or "")`.

3. **Presentation.** Change the signature to `render_page(count: int | None, latest: LatestRun | None = None) -> str`. The default keeps every existing call `render_page(n)` and its output byte-for-byte the same. `render_page` must not touch sqlite for run data. After the existing `{status}\n` line, emit the following, each line ending in `\n`:
   - `latest is None`: nothing.
   - `latest.outcome == ""`: `<p id="latest-run-not-ended">Latest run has not ended yet</p>`
   - otherwise: `<p>Latest run outcome: <strong id="latest-run-outcome">{outcome}</strong></p>`, followed, only if `latest.reason` is non-empty, by `<p>Latest run reason: <span id="latest-run-reason">{reason}</span></p>`.
   
   Both values go through `html.escape`.

4. **HTTP.** In `_Handler.do_GET`, change the one composition line to `render_page(count_runs(), latest_run())`. Nothing else changes.

5. Update the module docstring to mention the latest run's outcome and reason.

## Tests (`tests/test_viewer.py`)

All existing tests must pass unchanged. Their databases have only an `id` column, so `latest_run()` returns `None` and the page is identical to today.

Add a helper that creates `runs (id INTEGER PRIMARY KEY, outcome TEXT, reason TEXT)` and inserts given `(outcome, reason)` rows. Then add:

- **`latest_run`**
  - Returns `LatestRun("passed", "all green")` for a single ended run.
  - With several rows, returns the one with the highest id, including when that row is not ended and older rows are.
  - Returns `LatestRun("", "")` for a row with `NULL` columns, and the same for empty-string columns.
  - Returns `None` for each of: empty table, missing file, missing `runs` table, `runs` without `outcome`/`reason` columns, a non-sqlite file.
  - Opens read-only: exactly one `connect` call, with `mode=ro` and `uri=True`.
  - Leaves the file bytes and the directory listing unchanged, and does not create a missing database.
  - Sees a newly inserted row on the next call.
- **`render_page`**
  - `render_page(3)` equals `render_page(3, None)` and contains no `latest-run`.
  - An ended run renders the exact outcome and reason markup above.
  - An ended run with an empty reason has the outcome element and no `latest-run-reason`.
  - A not-ended run has `id="latest-run-not-ended"` and no `latest-run-outcome`.
  - An outcome or reason such as `<b>&` is escaped.
  - The count line is still present alongside the new lines.
- **HTTP**
  - `GET /` on a database with an ended latest run shows its outcome and reason, and the body equals `viewer.render_page(viewer.count_runs(), viewer.latest_run())`.
  - A not-ended latest run shows the not-ended line.
  - Updating the latest row to ended, or inserting a newer row, shows on the next request.
  - No database still returns 200 with no `latest-run` in the body.

## Docs

- `docs/viewer.md`: update the intro. Add a short section "Latest run" covering what is shown, that highest `id` means latest, the not-ended line, and that nothing is shown when there is no run or the columns cannot be read.
- `CHANGELOG.md`: add an entry under Unreleased / Added.

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

- The page now shows the outcome and reason of the run with the highest id, and shows 'Latest run has not ended yet' when that run's outcome is NULL or empty, which is what the request asked for.
- The change follows the approved plan step by step: the LatestRun value type, the shared read-only _fetch_one helper, count_runs rebuilt on it with unchanged behaviour, latest_run with the planned query, the planned markup with HTML escaping, and a one-line change in the HTTP handler.
- The layers stay separate with an explicit interface: the data layer hands the presentation layer only a LatestRun or None, the presentation code does not touch sqlite for run data, and the handler is still the only place they are joined.
- Existing tests are unchanged and all 118 tests pass in the factory run; the new tests cover every case the plan lists at the data, rendering and HTTP levels.
- The docs and changelog describe the new behaviour, including the cases where nothing is shown and the fact that count and latest run are read on separate connections.
- Not blocking: a non-text (BLOB) value in outcome or reason would cause a 500, which no test covers; the request states these are text columns.
- Not blocking: a renamed or missing outcome/reason column makes the latest-run lines disappear without a message; this is the plan's stated behaviour and is recorded as a backlog warning for later.

## Commits it made on main

- `8cb6209` factory: red (run 5)
- `530694a` factory: test (run 5)
- `3231443` factory: architecture_review (run 5)
- `3ac089b` factory: document (run 5)
