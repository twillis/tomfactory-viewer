# Run 3: needs-me

*change, started 2026-11-18 17:25 UTC, took 5m 23s, an estimated $2.96 of agent time.*

## The request

Show the most recent run's outcome and reason on the page. The runs table has text columns named outcome and reason, which are empty until a run has ended, and an integer id.

## Sent back (plan version 1)

Also show when that run ended. The runs table has a text column ended_at holding an ISO 8601 time.

## The approved plan (version 2)

# Plan: show the most recent run's outcome, reason and end time

Revision of the earlier plan. The only change is the operator's comment: the page also shows when the most recent run ended, read from the `ended_at` text column. Everything else is kept.

## Assumptions

- **"Most recent run" means the row in `runs` with the highest `id`.** It is not ordered by `ended_at`, because that column is empty for a run still in progress, and such a run is the most recent one.
- **"Empty" means `NULL` or the empty string `''`.**
- **A run has ended when its `outcome` is not empty.** `ended_at` is not used to decide this; it is only displayed.
- **If the most recent run has not ended, the page says so.** It does not fall back to an older, ended run, and it shows no outcome, reason or end time.
- **`ended_at` is shown exactly as stored** (HTML-escaped). It is not parsed, validated, reformatted or converted to another time zone. The request asks only to show it, and parsing would add failure cases for malformed values.
- **If there is no run to show, the page adds nothing.** This covers no rows, a missing or unreadable database, and a `runs` table lacking any of the `outcome`, `reason` or `ended_at` columns. The existing count line or "Run count unavailable" message already covers these cases, and the existing tests, whose databases have only an `id` column, keep passing byte-for-byte.

## Files to change

Only `viewer.py` changes in the implementation. No new files, no new dependencies (standard library only; an existing test enforces this).

### 1. `viewer.py`: new function `latest_run()`

```python
def latest_run() -> tuple[str, str, str] | None:
```

- Opens the database exactly as `count_runs()` does: `sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)`, closed in a `finally`, with `sqlite3.Error` caught and turned into `None`.
- Runs `SELECT outcome, reason, ended_at FROM runs ORDER BY id DESC LIMIT 1`.
- Returns `None` if the query fails (no file, not a database, no `runs` table, any of the three columns missing) or if there are no rows.
- Otherwise returns `(outcome, reason, ended_at)`, with each value converted by `"" if value is None else str(value)`. `NULL` therefore becomes `""` and all three elements are always `str`.
- Never creates or writes to the database.
- Leave `count_runs()` unchanged. An existing test asserts it makes exactly one `sqlite3.connect` call.

### 2. `viewer.py`: `render_page`

Change the signature to:

```python
def render_page(count: int | None, last_run: tuple[str, str, str] | None = None) -> str:
```

The second parameter must be optional, because existing tests call `render_page(3)`.

Build a `last_run_html` string and insert it immediately after the existing `f"{status}\n"` line, before `</body>`:

- **`last_run is None`:** `""`. The output is byte-identical to today's page.
- **`last_run[0] == ""` (not ended):**
  ```
  <p id="last-run-not-ended">Most recent run has not ended yet.</p>\n
  ```
  Nothing else is added, even if `reason` or `ended_at` happens to be set.
- **Otherwise (ended):** three lines in this order, all values passed through `html.escape`:
  ```
  <p>Most recent run outcome: <strong id="last-run-outcome">{outcome}</strong></p>\n
  <p>Most recent run reason: <span id="last-run-reason">{reason}</span></p>\n
  <p>Most recent run ended at: <time id="last-run-ended-at">{ended_at}</time></p>\n
  ```
  If the outcome is set but the reason or `ended_at` is empty, that line is still rendered with an empty element.

The count/unavailable block and everything else on the page stay exactly as they are. Update the function docstring.

### 3. `viewer.py`: `_Handler.do_GET`

Change the body line to:

```python
body = render_page(count_runs(), latest_run()).encode("utf-8")
```

Both values are read afresh on every request. Update the module docstring to mention the most recent run's outcome, reason and end time.

### 4. Docs (documenter step)

- `docs/viewer.md`: add to the intro and to "Where it reads from" that the page shows the outcome, reason and end time (`ended_at`, shown as stored) of the run with the highest `id`. Describe the "has not ended yet" message. State that nothing extra is shown when there are no runs or when any of the `outcome`, `reason` or `ended_at` columns is missing.
- `CHANGELOG.md`: add an entry under Unreleased / Added.

## How tests can show it works (`tests/test_viewer.py`)

Add a helper that creates `factory.db` with `CREATE TABLE runs (id INTEGER PRIMARY KEY, outcome TEXT, reason TEXT, ended_at TEXT)` and inserts given `(id, outcome, reason, ended_at)` rows. Leave the existing `make_db` and `insert_run` alone.

**`latest_run()`**

- Returns the `(outcome, reason, ended_at)` of the highest-`id` row, even when rows are inserted out of id order, e.g. ids 5, 2, 9 gives row 9.
- Chooses by `id`, not by `ended_at`: with id 1 ended at `2026-11-18T10:00:00Z` and id 2 ended at `2026-11-17T10:00:00Z`, it returns row 2.
- Returns `("", "", "")` when the latest row has `NULL` outcome, reason and ended_at, and also when all are `''`, even if an older row has ended.
- Returns `("failed", "", "")` when the outcome is set and reason and ended_at are `NULL`.
- Returns `ended_at` unchanged, e.g. `"2026-11-18T09:30:00+00:00"` comes back as exactly that string.
- Returns `None` for each of: an empty `runs` table, a missing database file, a file that is not SQLite, no `runs` table, a `runs` table with only `id` (the existing `make_db`), and a `runs` table with `id, outcome, reason` but no `ended_at`.
- Does not create a missing database or home directory, and leaves the file bytes unchanged.
- Opens with `mode=ro` and `uri=True` (same recording-`connect` technique as the existing `count_runs` test).
- Sees a newly inserted run on the next call.

**`render_page`**

- `render_page(3, ("passed", "all tests green", "2026-11-18T09:30:00Z"))` contains `<strong id="last-run-outcome">passed</strong>`, `<span id="last-run-reason">all tests green</span>` and `<time id="last-run-ended-at">2026-11-18T09:30:00Z</time>`, does not contain `last-run-not-ended`, and still contains the run count.
- `render_page(3, ("", "", ""))` contains `<p id="last-run-not-ended">Most recent run has not ended yet.</p>` and none of `last-run-outcome`, `last-run-reason`, `last-run-ended-at`.
- `render_page(3, ("passed", "ok", ""))` contains `<time id="last-run-ended-at"></time>`.
- `render_page(3, None) == render_page(3)`, and it contains no `last-run`.
- Escaping: `render_page(1, ("<b>x</b>", "a&b <script>", "<i>now</i>"))` contains the `html.escape`d forms and no raw `<b>`, `<script>` or `<i>`.
- `render_page(None, ("passed", "ok", "2026-11-18T09:30:00Z"))` shows both the unavailable message and the three run lines. The two blocks are independent.

**HTTP**

- With a database whose latest run has ended, `GET /` returns 200 and the body equals `viewer.render_page(count, (outcome, reason, ended_at))`.
- With the latest run not ended, the body contains `last-run-not-ended` and no `last-run-ended-at`.
- After updating the latest row's outcome, reason and ended_at, or inserting a newer ended row, while the server is running, the next `GET /` shows the new values.
- With a database that has only an `id` column, or no database, the body contains no `last-run`.

**Regression**

- All existing tests pass unmodified. In particular, `test_get_root_body_is_the_rendered_page` still holds because `latest_run()` is `None` for an id-only table.

Run with `python -m pytest -q -p no:cacheprovider`.

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
| architecture_review | 1 | error |
| document | 1 | error |

**Outcome: needs-me.** the agent program failed during the document step: the agent program reported a usage limit: allowed_warning

Nothing from this run was merged.
