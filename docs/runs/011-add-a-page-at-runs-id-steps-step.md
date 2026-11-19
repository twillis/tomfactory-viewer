# Run 11: accepted

*change, started 2026-11-19 04:22 UTC, took 369m 46s, an estimated $10.07 of agent time.*

> **Operator's note.** Stopped by the usage limit at the document step. The factory gained `factory resume`, which carried this run on from where it stopped: only documentation and review were run again.

## The request

Add a page at /runs/<id>/steps/<step id> showing one step in full: what it was asked, what it produced, each check with its note, and its events in order with time, kind and summary. The steps table on the run page links to it. An unknown step, or one that belongs to another run, gives a 404 page. The database schema, including what the JSON columns hold, is documented in docs/run-store.md; read it. Use only the Python standard library.

## The approved plan (version 1)

# Plan: a page for one step at `/runs/<id>/steps/<step id>`

## Files

- `viewer.py` — changed: data layer, presentation layer, one new route. No new import; standard library only.
- `tests/test_viewer.py` — changed by the test writer (see Tests).
- `docs/viewer.md`, `CHANGELOG.md` — the documenter adds a "One step" section and a changelog entry.

No new file. The module docstring is updated to say four pages and to name `StepPageData`.

## Data layer (`viewer.py`)

1. **`Check` gains a third field, `note: str = ""`.** `_parse_checks` fills it with the stored `note` when that is text, otherwise `""`. What makes checks `UNREADABLE` is unchanged, so the run page behaves as before.
2. **`_parse_json(value, kind)`** (new, replaces the first half of `_parse_checks`): returns `kind()` for `None`; `UNREADABLE` if the value is not `str`, is not JSON (`ValueError`, `RecursionError`), or the parsed value is not an instance of `kind`; otherwise the parsed value. `_parse_checks` calls it with `list`; `steps.produced` is parsed with `_parse_json(value, dict)`.
3. **New hand-off types** (NamedTuples, fields in this order):
   - `Event(id: int, at: datetime | None, kind: str, summary: str)`
   - `StepDetail(summary: StepSummary, asked: str, produced: dict | Unreadable)` — `produced` is `{}` when the column is NULL, with keys in stored order.
   - `StepPageData(run_id: int, step_id: int, step: StepDetail | None | Unreadable, events: tuple[Event, ...] | Unreadable, read_at: datetime)` — `run_id` and `step_id` are the ids asked for; `step` is `None` only when `steps` was read and has no such row for that run.
4. **`_STEP_COLUMNS = "id, name, persona, attempt, started_at, ended_at, passed, checks"` and `_step_summary(row) -> StepSummary`**, which converts those eight columns. `read_run_page_data` uses both in place of its inline column list and conversion.
5. **`_read_one_and_its_rows(one_sql, rows_sql, params)`**: the one place that does "open read-only, `BEGIN`, read one row, then the rows under it, close once". Both statements take the same `params`. It returns `(row, rows)`:
   - `(UNREADABLE, UNREADABLE)` if the connect, the `BEGIN` or the first statement fails;
   - `(None, ())` if the first statement finds no row (the second statement is not run);
   - `(row, UNREADABLE)` if only the second statement fails;
   - otherwise `(row, list_of_rows)`.
   
   `read_run_page_data` is rewritten on top of it with the same results as now. This is the helper `docs/backlog.md` asks for before a fourth read is written.
6. **`read_step_page_data(run_id: int, step_id: int) -> StepPageData`**, with `read_at` taken first as in the other readers, and `params = (step_id, run_id)`:
   - step: `SELECT {_STEP_COLUMNS}, asked, produced FROM steps WHERE id = ? AND run_id = ?`
   - events: `SELECT id, at, kind, summary FROM events WHERE step_id = ? AND run_id = ? ORDER BY id`
   
   The one `WHERE` covers an unknown step, a step of another run and an unknown run: all give `step=None, events=()`. Conversions: `asked or ""`; `kind or ""`; `summary or ""`; `at` through `_parse_time`. `events.payload` is not read; run-store.md says to show `summary`.

## Presentation layer (`viewer.py`)

1. **Link from the run page.** `_render_steps` and `_render_step_row` take `run_id` as a new first argument, passed from `render_run_page`. The name cell becomes `<td class="step-name"><a href="/runs/{run_id}/steps/{step.id}">{escaped name}</a></td>`. Nothing else in the row changes.
2. **Two small helpers taken out of `_render_step_row`** and used by both pages:
   - `_step_result(step)` gives `not finished`, `passed` or `failed`;
   - `_step_duration(step, read_at)` gives the formatted duration, with ` so far` when it is known and the step is unfinished.
3. **`render_step_page(data: StepPageData) -> str`**, built with `_document`. All stored text is `html.escape`d. Every line ends in a newline.
   - `step is None`: title `Step not found - tomfactory viewer`, then `<h1>Step not found</h1>` and `<p id="step-not-found">No step with id {step_id} in run {run_id}</p>`.
   - Otherwise the title is `Step {step_id} of run {run_id} - tomfactory viewer` and the heading is `<h1>Step {step_id} of run {run_id}</h1>`.
   - `step is UNREADABLE`: the body is `<p id="step-unavailable">Step unavailable: could not read the steps table</p>`.
   - Otherwise the body is these lines, in this order:
     - `<dl id="step">`
     - `<dt>Name</dt><dd id="step-name">{name}</dd>`
     - `<dt>Persona</dt><dd id="step-persona">{persona, or none when empty}</dd>`
     - `<dt>Attempt</dt><dd id="step-attempt">{attempt}</dd>`
     - `<dt>Result</dt><dd id="step-result">{_step_result}</dd>`
     - `<dt>Duration</dt><dd id="step-duration">{_step_duration}</dd>`
     - `</dl>`
     - `<h2>Asked</h2>`
     - `<pre id="step-asked">{asked}</pre>`
     - `<h2>Produced</h2>`, then the produced block
     - `<h2>Checks</h2>`, then the checks block
     - `<h2>Events</h2>`, then the events block
   - **Produced block** (`_render_produced`):
     - `UNREADABLE`: `<p id="produced-unavailable">Produced unavailable: could not read what the step produced</p>`
     - `{}`: `<p id="no-produced">Nothing produced</p>`
     - otherwise `<dl id="step-produced">`, one line per key in stored order `<dt>{key}</dt><dd><pre>{text}</pre></dd>`, then `</dl>`. `text` is the value itself when it is text, otherwise `json.dumps(value, indent=2, ensure_ascii=False)`.
   - **Checks block** (`_render_check_rows`):
     - `UNREADABLE`: `<p id="checks-unavailable">Checks unavailable: could not read the checks of this step</p>`
     - `()`: `<p id="no-checks">No checks recorded</p>`
     - otherwise `<table id="checks">`, `<thead>`, `<tr><th>Name</th><th>Result</th><th>Note</th></tr>`, `</thead>`, `<tbody>`, one line per check in order `<tr><td class="check-name">{name}</td><td class="check-result">{passed or failed}</td><td class="check-note"><pre>{note}</pre></td></tr>`, then `</tbody>`, `</table>`.
   - **Events block** (`_render_events`):
     - `UNREADABLE`: `<p id="events-unavailable">Events unavailable: could not read the events table</p>`
     - `()`: `<p id="no-events">No events recorded</p>`
     - otherwise `<table id="events">`, `<thead>`, `<tr><th>Time</th><th>Kind</th><th>Summary</th></tr>`, `</thead>`, `<tbody>`, one line per event in the order given `<tr id="event-{id}"><td class="event-time">{_format_time(at)}</td><td class="event-kind">{kind}</td><td class="event-summary">{summary}</td></tr>`, then `</tbody>`, `</table>`.

## HTTP layer (`viewer.py`)

- `_step_route(run_id, step_id)`: reads with `read_step_page_data(int(run_id), int(step_id))` and answers 404 when `data.step is None`, else 200, with `render_step_page(data)`.
- One new `_ROUTES` entry: `/runs/([1-9][0-9]{0,17})/steps/([1-9][0-9]{0,17})`. `_Handler` is unchanged.
- An unreadable database answers 200 with the unavailable line, as the other pages do.

## Tests (`tests/test_viewer.py`, within the 300-line budget, so tables of cases)

Updates to what exists:
- `step_row` expects the link (run 7).
- The hand-off fields table gains `Check` = `name passed note`, plus `Event`, `StepDetail` and `StepPageData`.
- `facts` also leaves out `step_id`.
- Add `EVENTS_DDL` copied from `docs/run-store.md`, `asked` and `produced` arguments on `insert_step`, and an `insert_event` helper; `make_run_db` creates the events table.

New cases, mostly rows in existing tables:
- `READERS` gains `read_step_page_data(7, 1)`. It then inherits the tests for unreadable databases, one read-only connection closed once, no file changes, fresh reads and `read_at`.
- `FAILING_STATEMENTS` gains two rows: `from steps` fails, so both facts are `UNREADABLE`; `from events` fails, so the step is kept and events are `UNREADABLE`.
- `read_step_page_data` maps every field, including a check's note and events ordered by id, not insertion or time. Events of other steps are left out.
- One table for a missing step: unknown step id, step of another run, unknown run. Each gives `step is None`, `events == ()` and no statement on `events`.
- One table for `produced`: NULL gives `{}`; an object is kept in key order; not JSON, a list, a number or a blob give `UNREADABLE`.
- One table for `note`: text is kept; missing, null or a number give `""`.
- `render_step_page` is exactly the planned document for: a full step, the not-found page, an unreadable step, and each empty or unreadable block. One escaping test covers every stored text at once. A non-text produced value is shown as indented JSON.
- The `RENDERS_WITHOUT_THE_DATABASE` table gains a step page row.
- HTTP: `PAGES` gains `/runs/7/steps/1`, equal to `render_step_page(read_step_page_data(7, 1))`. A table of 404s with the not-found page covers `/runs/7/steps/99`, a step of run 8 asked for under run 7, and `/runs/99/steps/1`.
- The not-served paths gain `/runs/7/steps/`, `/runs/7/steps/0`, `/runs/7/steps/abc` and `/runs/7/steps/1/`.
- The link-walking test follows the step link from the run page and gets 200.

## Assumptions

- "In full" is read as the four things named plus the facts that identify the step: name, persona, attempt, result and duration, all already read for the run page. Left out because not asked for: kind, failure, tokens, cost, a check's `timed_out`, event payloads, a link back to the run, paging of events.
- One 404 message serves all three causes (unknown step, step of another run, unknown run); the page does not say which.
- Every event kind is listed. The page shows kind and summary without interpreting them, so run-store guarantee 4 (ignore unknown kinds) is not at stake.
- Event times use the existing `_format_time`, so they show whole seconds; order within a second is carried by row order.
- `viewer.py` stays one file. The backlog warns that the layers cannot become modules while the import test rejects this codebase's own modules; fixing that was not asked for and is left alone.

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
| document | 1 | error |
| document | 2 | passed |
| review | 1 | passed |
| finish | 1 | passed |

**Outcome: accepted.** the tests pass and the review found the result matches the request, the plan and the ideals

## The review

Verdict: **accept**

- The page at /runs/<id>/steps/<step id> shows what the step was asked, what it produced, each check with its note, and its events in id order with time, kind and summary, as the request asked.
- The step name in the run page's steps table links to the step page, and a test follows that link to a 200.
- An unknown step, a step of another run and an unknown run all answer 404 with the not-found page, tested at the data and HTTP levels.
- The JSON columns are read as docs/run-store.md describes them, and viewer.py imports only the standard library.
- The change follows the approved plan; the one departure is an extra helper, _render_step_facts, which mirrors the existing _render_run_facts and leaves the output unchanged.
- No ideal is broken: the data, presentation and HTTP layers stay separate behind StepPageData, the run reader was reduced onto shared helpers, and the new tests are tables of cases.
- I could not run the tests myself; the 386 passed is the factory's result.
- To fix later, not blocking: the new 'One step' section of docs/viewer.md says a time not written exactly as 2026-11-18T21:04:05Z shows 'unknown', but the code also reads the millisecond form the factory writes.
- To fix later, not blocking: tests/test_viewer.py lost the space after '=' in the CREATED constant, a line the change had no reason to touch.

## Commits it made on main

- `74ca5d9` factory: red (run 11)
- `224ca22` factory: test (run 11)
- `029709e` factory: architecture_review (run 11)
- `49aadee` factory: document (run 11)
