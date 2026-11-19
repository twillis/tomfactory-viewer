# Run 2: rejected

*change, started 2026-11-18 17:13 UTC, took 5m 38s, an estimated $3.06 of agent time.*

## The request

Show the most recent run's outcome and reason on the page. The runs table has text columns named outcome and reason, which are empty until a run has ended, and an integer id.

## Sent back (plan version 1)

Also show when that run ended. The runs table has a text column ended_at holding an ISO 8601 time.

## The approved plan (version 2)

# Plan: show the most recent run's outcome, reason and end time

## Assumptions

- "Most recent run" means the row in `runs` with the highest `id`, whether or not it has ended. The alternative reading (the most recent run that has ended) is not planned.
- "Empty" means `NULL` or the empty string. If the most recent run's `outcome` is empty, the page says the run has not ended instead of showing blank values, and shows no end time.
- `ended_at` is shown exactly as stored (for example `2026-11-18T09:30:00+00:00`), HTML-escaped. It is not parsed, validated, reformatted or converted to another time zone.
- `outcome`, `reason` and `ended_at` are read with one query. If the latest run cannot be read (no database, no `runs` table, any of the three columns missing, or no rows), the page shows nothing extra and is byte-for-byte what it is today. The existing test `test_get_root_body_is_the_rendered_page` requires this for a `runs` table with only an `id` column: it expects the body to equal `render_page(3)`. A consequence: a `runs` table that has `outcome` and `reason` but no `ended_at` shows nothing about the latest run.

## Files to change

### `viewer.py` (the only implementation file)

1. **Add `latest_run() -> tuple[str, str, str] | None`**, next to `count_runs()`.
   - Open the database exactly as `count_runs()` does: `sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)`, closed in a `finally`.
   - Run `SELECT outcome, reason, ended_at FROM runs ORDER BY id DESC LIMIT 1`.
   - Return `None` on any `sqlite3.Error` or when there is no row.
   - Otherwise return `(outcome, reason, ended_at)`, with `NULL` turned into `""` and any other value passed through `str()`.
   - Do not change `count_runs()`; an existing test asserts it makes exactly one `sqlite3.connect` call.

2. **Change `render_page` to `render_page(count: int | None, latest: tuple[str, str, str] | None = None) -> str`.** The second argument must be optional, because existing tests call `render_page(3)`. Build a `latest_html` string and insert it right after the `f"{status}\n"` line:
   - `latest is None`: `latest_html = ""`, so the output is identical to today's.
   - `latest[0] == ""` (not ended): `'<p id="latest-run-not-ended">Most recent run has not ended yet</p>\n'`
   - otherwise, with all three values passed through `html.escape`, these three lines in this order:
     ```
     <p>Most recent run outcome: <strong id="latest-run-outcome">{outcome}</strong></p>\n
     <p>Most recent run reason: <span id="latest-run-reason">{reason}</span></p>\n
     <p>Most recent run ended at: <time id="latest-run-ended-at">{ended_at}</time></p>\n
     ```
     An empty reason or empty `ended_at` with a non-empty outcome renders an empty element. The `<time>` element has no `datetime` attribute.

3. **In `_Handler.do_GET`**, change the call to `render_page(count_runs(), latest_run())`.

4. Update the module docstring to mention the latest run's outcome, reason and end time. No new imports.

### `tests/test_viewer.py` (test writer)

Add a helper that creates `factory.db` with `CREATE TABLE runs (id INTEGER PRIMARY KEY, outcome TEXT, reason TEXT, ended_at TEXT)` and inserts given `(outcome, reason, ended_at)` rows. Leave the existing `make_db` and all existing tests as they are; they must still pass.

`latest_run`:
- Rows `("passed", "all green", "2026-11-17T10:00:00+00:00")` then `("failed", "tests red", "2026-11-18T09:30:00+00:00")` return `("failed", "tests red", "2026-11-18T09:30:00+00:00")`.
- With explicit ids inserted out of order, the highest id wins, even when a lower id has a later `ended_at`.
- Latest row with `NULL` outcome, reason and ended_at returns `("", "", "")`; the same for empty strings.
- `ended_at` is returned unchanged, character for character.
- Returns `None` for: an empty `runs` table, a missing database file, a missing `runs` table, a `runs` table with only `id`, a `runs` table with `outcome` and `reason` but no `ended_at`, and a file that is not a SQLite database.
- Connects with `mode=ro` and `uri=True`, leaves the file bytes unchanged, and does not create a missing file or directory.
- Sees a row inserted between two calls.

`render_page`:
- `render_page(3)` and `render_page(3, None)` are equal and contain no `latest-run` text.
- `render_page(3, ("failed", "tests red", "2026-11-18T09:30:00+00:00"))` contains `<strong id="latest-run-outcome">failed</strong>`, `<span id="latest-run-reason">tests red</span>` and `<time id="latest-run-ended-at">2026-11-18T09:30:00+00:00</time>`, still contains the run count, and does not contain `latest-run-not-ended`.
- `render_page(3, ("", "", ""))` contains `<p id="latest-run-not-ended">Most recent run has not ended yet</p>` and none of `latest-run-outcome`, `latest-run-reason`, `latest-run-ended-at`.
- `render_page(3, ("failed", "tests red", ""))` contains `<time id="latest-run-ended-at"></time>`.
- `render_page(1, ("<b>x</b>", "a&b", "<i>t</i>"))` contains the escaped forms of all three and not the raw `<b>` or `<i>`.

HTTP:
- `GET /` with an ended latest run shows its outcome, reason and end time, and the body equals `render_page(count, (outcome, reason, ended_at))`.
- `GET /` when the latest run has not ended shows `latest-run-not-ended` and no `latest-run-ended-at`, even if an earlier run has ended.
- After a newer ended run is inserted while serving, the next `GET /` shows the new outcome, reason and end time.
- `GET /` leaves the database file unchanged.

### `docs/viewer.md` and `CHANGELOG.md` (documenter)

- `docs/viewer.md`: say the page also shows the outcome, reason and end time of the run with the highest `id`; that the end time is the `ended_at` text exactly as stored; that it shows "Most recent run has not ended yet" when that run's outcome is empty; and that it shows nothing for the latest run when it cannot be read, including when any of the `outcome`, `reason` or `ended_at` columns is missing.
- `CHANGELOG.md`: one line under Unreleased / Added covering outcome, reason and end time. List the "not ended yet" message under "Beyond the request".

## How to check

Run `python -m pytest -q -p no:cacheprovider`. All existing tests and the new ones pass.

## How it went

| Step | Attempt | Result |
|------|---------|--------|
| plan | 1 | passed |
| plan | 2 | passed |
| baseline | 1 | passed |
| write_tests | 1 | passed |
| red | 1 | passed |
| build | 1 | passed |
| test | 1 | passed |
| document | 1 | passed |
| review | 1 | check |

**Outcome: rejected.** The request asks for the most recent run's outcome and reason and describes the runs table as having id, outcome and reason. On exactly that table the page shows neither, because outcome, reason and ended_at are read in one query that fails when ended_at is missing.; The end time is an addition the request did not ask for (the changelog lists it under 'Beyond the request'), and as built it can switch off the requested behaviour. The tests assert that a table with outcome and reason but no ended_at shows nothing about the latest run.; The implementation follows the approved plan faithfully, so the fault is in the plan: it makes the requested outcome and reason depend on a column the request never mentioned. Nothing in the repository outside this change shows that ended_at exists, and I could not read the live factory.db to check.; To fix: show outcome and reason whenever those two columns are readable, and either drop the end time or show it only when an ended_at column exists (for example, fall back to a query without ended_at). Update the tests and docs that currently assert the all-or-nothing behaviour.; Alternatively, if the operator confirms the live runs table has an ended_at column and wants the end time shown, the change is acceptable as it stands: it matches the plan, escapes all values, opens the database read-only, leaves the page unchanged when the run cannot be read, and all 134 tests pass.

## The review

Verdict: **reject**

- The request asks for the most recent run's outcome and reason and describes the runs table as having id, outcome and reason. On exactly that table the page shows neither, because outcome, reason and ended_at are read in one query that fails when ended_at is missing.
- The end time is an addition the request did not ask for (the changelog lists it under 'Beyond the request'), and as built it can switch off the requested behaviour. The tests assert that a table with outcome and reason but no ended_at shows nothing about the latest run.
- The implementation follows the approved plan faithfully, so the fault is in the plan: it makes the requested outcome and reason depend on a column the request never mentioned. Nothing in the repository outside this change shows that ended_at exists, and I could not read the live factory.db to check.
- To fix: show outcome and reason whenever those two columns are readable, and either drop the end time or show it only when an ended_at column exists (for example, fall back to a query without ended_at). Update the tests and docs that currently assert the all-or-nothing behaviour.
- Alternatively, if the operator confirms the live runs table has an ended_at column and wants the end time shown, the change is acceptable as it stands: it matches the plan, escapes all values, opens the database read-only, leaves the page unchanged when the run cannot be read, and all 134 tests pass.

Nothing from this run was merged.
