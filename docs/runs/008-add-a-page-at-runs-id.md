# Run 8: needs-me

*change, started 2026-11-18 19:35 UTC, took 20m 31s, an estimated $11.28 of agent time.*

> **Operator's note.** Stopped by the usage limit at the document step, after the code was built and its tests passed. Checked by hand (all 776 tests passed; the page worked against the real database) and merged without the factory's own review.

## The request

Add a page at /runs/<id> showing one run: its request, target, branch, state or outcome with the reason, attempt limit, and a table of its steps in order with name, persona, attempt, how long it took, passed or failed, and the names of its checks marking any that failed. Each run on /runs links to it. An unknown id gives a 404 page. The database schema is documented in docs/run-store.md in this codebase; read it. Use only the Python standard library.

## The approved plan (version 1)

# Plan: a page for one run at `/runs/<id>`

This codebase does not use spec kit. All code is in `viewer.py`, in three layers: data (`read_*`), presentation (`render_*`) and HTTP (`_Handler`). The change keeps those layers. No two rules disagree for this request.

## Facts the builder needs that the checkout's `docs/run-store.md` gets wrong

The operator's answers override the document:

- `steps.checks` is NULL until the step finishes. After that it is a JSON list, in the order the checks ran. Each item is an object with `name` (text), `passed` (true or false), `note` (text) and `timed_out` (true or false). A check failed when `passed` is false.
- Timestamps come as `2026-11-18T21:04:05Z` and as `2026-11-18T21:04:05.123Z`. Both must parse.

## Assumptions

- `<id>` is a positive decimal integer with no leading zero and at most 18 digits, so it always fits SQLite's 64-bit integer. Anything else under `/runs/` (`/runs/`, `/runs/abc`, `/runs/0`, `/runs/07`, `/runs/7/`) is not a route: it gets the existing plain 404 and the database is not opened.
- A well-formed id with no row gives the viewer's own 404 page.
- If the database or a needed column cannot be read, the page answers 200 with an unavailable line, as the other two pages do.
- The link on `/runs` goes on the id in the Id cell.
- The reason is shown only when the run has ended and the reason is not empty, the same rule the home page uses.
- A step that has not finished shows the time since it started, followed by `so far`, and `not finished` as its result. A step left unfinished by a killed driver keeps growing; the documenter lists this under things to know.
- Not shown, because not asked for: created and ended times of the run, a check's `note` and `timed_out`, a step's `failure` kind, and links back to `/runs`.
- Not done: splitting `viewer.py` into one module per layer, and the shared read helper. Both stay in `docs/backlog.md`. Existing readers are left as they are.

## `viewer.py`: data layer

Add imports `json` and `re`.

1. `_parse_time`: try `%Y-%m-%dT%H:%M:%SZ`, then `%Y-%m-%dT%H:%M:%S.%fZ`; return None if neither fits. This is the one place the stored format is known, so `/runs` gains it too.
2. New value types, all `NamedTuple`:
   - `Check(name: str, passed: bool)`.
   - `StepSummary(id: int, name: str, persona: str, attempt: int, started_at: datetime | None, ended_at: datetime | None, passed: bool | None, checks: tuple[Check, ...] | Unreadable)`. `persona` is `""` when NULL. `passed` is None until the step finishes. `checks` is `()` when the column is NULL.
     - Property `finished`: `passed is not None`.
     - Method `duration(now) -> timedelta | None`, the same rule as `RunSummary.duration`: None without `started_at`; if finished, `ended_at - started_at` (None without `ended_at`); otherwise `now - started_at`; never negative.
   - `RunDetail(summary: RunSummary, target: str, branch: str, reason: str, attempt_limit: int)`. It holds a `RunSummary` so the rules `ended` and `status` stay stated once. `branch` and `reason` are `""` when NULL.
   - `RunPageData(run_id: int, run: RunDetail | None | Unreadable, steps: tuple[StepSummary, ...] | Unreadable, read_at: datetime)`. `run` is None only when the `runs` table was read and has no row with that id.
3. `_parse_checks(value) -> tuple[Check, ...] | Unreadable`: None gives `()`. Otherwise `json.loads`; the result must be a list whose every item is a dict with a `str` `name` and a `bool` `passed`. Anything else, including text that is not JSON, gives `UNREADABLE` for that step only.
4. `read_run_page_data(run_id: int) -> RunPageData`, in the shape of `read_page_data`: take `read_at`, open one read-only connection with `_connect()`, `BEGIN`, close in `finally`, use only `execute` and `close` on the connection.
   - `SELECT id, state, outcome, request, created_at, ended_at, target, branch, reason, attempt_limit FROM runs WHERE id = ?`.
   - On `sqlite3.Error` from connect, `BEGIN` or this statement: `run` and `steps` are both `UNREADABLE`.
   - No row: `run` is None, `steps` is `()`, and the steps statement is not run.
   - Otherwise `SELECT id, name, persona, attempt, started_at, ended_at, passed, checks FROM steps WHERE run_id = ? ORDER BY id`. On `sqlite3.Error`, `steps` is `UNREADABLE` and the run is kept. `passed` becomes None for NULL, else `bool(value)`.

## `viewer.py`: presentation layer

`render_run_page(data: RunPageData) -> str`. It reads no database, no clock and not `db_path()`. All stored text goes through `html.escape`. It uses `_document` and `_format_duration`. Body lines, each ending in a newline:

- **Run is None**: title `Run not found - tomfactory viewer`, then
  ```
  <h1>Run not found</h1>
  <p id="run-not-found">No run with id 99</p>
  ```
- **Run is `UNREADABLE`**: title `Run 7 - tomfactory viewer`, then
  ```
  <h1>Run 7</h1>
  <p id="run-unavailable">Run unavailable: could not read the runs table</p>
  ```
- **Otherwise**: title `Run 7 - tomfactory viewer`, then
  ```
  <h1>Run 7</h1>
  <dl id="run">
  <dt>Request</dt><dd id="run-request">the full request</dd>
  <dt>Target</dt><dd id="run-target">/path/to/target</dd>
  <dt>Branch</dt><dd id="run-branch">factory/7-add-a-page</dd>
  <dt>Outcome</dt><dd id="run-status">accepted</dd>
  <dt>Reason</dt><dd id="run-reason">all checks passed</dd>
  <dt>Attempt limit</dt><dd id="run-attempt-limit">3</dd>
  </dl>
  ```
  - The status label is `Outcome` when `summary.ended`, else `State`; the value is `summary.status`.
  - The Reason line is left out unless the run has ended and `reason` is not empty.
  - An empty branch shows `none`.
  - The steps follow, as one of:
    - `steps` is `UNREADABLE`: `<p id="steps-unavailable">Steps unavailable: could not read the steps table</p>`
    - no steps: `<p id="no-steps">No steps recorded yet</p>`
    - otherwise a table, rows in the order given:
      ```
      <table id="steps">
      <thead>
      <tr><th>Name</th><th>Persona</th><th>Attempt</th><th>Duration</th><th>Result</th><th>Checks</th></tr>
      </thead>
      <tbody>
      <tr id="step-12"><td class="step-name">test</td><td class="step-persona">tester</td><td class="step-attempt">2</td><td class="step-duration">3m 7s</td><td class="step-result">failed</td><td class="step-checks"><span class="check">guard</span>, <span class="check check-failed">tests (failed)</span></td></tr>
      </tbody>
      </table>
      ```
  - Duration is `_format_duration(step.duration(read_at))`, with ` so far` added when the step is not finished and the duration is known.
  - Result is `passed`, `failed`, or `not finished` when `passed` is None.
  - Checks are joined with `, ` in stored order. A failed check has the extra class and ` (failed)` after its name, so the mark shows without a stylesheet. No checks gives an empty cell. `UNREADABLE` checks give the cell text `unreadable`.

In `_render_run_row`, the Id cell becomes `<td class="run-id"><a href="/runs/7">7</a></td>`. Nothing else on `/runs` changes.

## `viewer.py`: HTTP layer

Replace `_PAGES` with an explicit route table, the shape the run 7 backlog entry asks for:

- `Response(status: int, body: str)`, a `NamedTuple`.
- `_ROUTES`: a tuple of `(compiled pattern, function)` pairs. Each function takes the pattern's groups as strings and returns a `Response`.
  - `/` gives `Response(200, render_page(read_page_data()))`.
  - `/runs` gives `Response(200, render_runs_page(read_runs_page_data()))`.
  - `/runs/([1-9][0-9]{0,17})` reads `data = read_run_page_data(int(group))` and gives `Response(404 if data.run is None else 200, render_run_page(data))`.
- `do_GET`: strip the query string, `fullmatch` each pattern in order, and call the first that fits. If none fits, `send_error(404)` as now, without opening the database. Otherwise send `response.status` with the same three headers and the body as now.

Update the module docstring to name the third page and `RunPageData`.

## Tests (`tests/test_viewer.py`)

Existing tests that must change:

- `run_row` and the regular expression in `test_get_runs_shows_a_running_run_with_its_state_and_duration_so_far`: the Id cell now holds the link.
- `test_get_runs_is_served_only_at_exactly_runs_not_at`: drop `/runs/7`.
- `MALFORMED_TIMES`: drop `fractional_seconds`; add a test that `.250Z` and `.123Z` parse, keeping the fraction.

New tests build the database from `RUNS_DDL` plus the `steps` DDL from `docs/run-store.md`. They show:

- **Data**: every field of a run and its steps is mapped. Steps come in `id` order and only for that run. NULL `persona`, `branch`, `reason` and `checks` map as above. `passed` 1, 0 and NULL become True, False and None. Checks parse in order. Bad checks JSON (not JSON, not a list, an item missing `name` or with a non-boolean `passed`) gives `UNREADABLE` for that step and leaves the others intact. An unknown id gives `run` None and `steps` `()` without running a statement on `steps`. A missing database or `runs` table gives both `UNREADABLE`; a missing `steps` table gives the run with `steps` `UNREADABLE`. One read-only connection is opened and closed once, and the file is unchanged. Both timestamp forms give durations.
- **Rules**: `StepSummary.finished` and `duration`, covering finished, unfinished, missing times and never negative.
- **Presentation**: exact documents for a run with steps, with no steps, with unreadable steps, for an unreadable run and for not found. `Outcome` versus `State`. Reason present and absent. Branch `none`. A failed check is marked and a passed one is not. `so far` and `not finished`. Every stored text is escaped. `render_run_page` does not open the database or call `db_path`.
- **HTTP**: `GET /runs/7` answers 200 with a body equal to `render_run_page(read_run_page_data(7))` for an ended run. `GET /runs/99` answers 404 as `text/html` with `id="run-not-found"` and a matching `Content-Length`. `/runs/`, `/runs/abc`, `/runs/0`, `/runs/07`, `/runs/7/` and a 19-digit id answer 404 without opening the database. `/runs/7?x=1` answers 200. Every row on `/runs` has `href="/runs/<id>"` and following it answers 200. `/` and `/runs` are unchanged apart from the link. The standard-library import test still passes.

## Documentation

- `docs/viewer.md`: add a section for the run page; update the list of pages, the Id column, the timestamp note (both forms now parse) and the Limits line that says `/runs/7` is a 404.
- `docs/run-store.md`: add a short note with the two corrections above (the shape of `checks`, milliseconds in timestamps).
- `CHANGELOG.md`: record the page, the link, the timestamp change and the new route table.
- `docs/backlog.md`: remove the two entries this change settles (the `_PAGES` shape and the `_parse_time` forms).

## How it went

| Step | Attempt | Result |
|------|---------|--------|
| plan | 1 | check |
| answer_question | 1 | passed |
| plan | 2 | passed |
| baseline | 1 | passed |
| write_tests | 1 | passed |
| red | 1 | passed |
| build | 1 | passed |
| test | 1 | passed |
| architecture_review | 1 | error |
| document | 1 | error |

**Outcome: needs-me.** the agent program failed during the document step: You've hit your 5-hour limit · resets 6:20pm (America/New_York)

## Commits it made on main

- `776f4a9` factory: red (run 8)
- `e5525d3` factory: test (run 8)
