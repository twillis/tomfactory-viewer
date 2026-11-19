# Run 13: accepted

*change, started 2026-11-19 10:45 UTC, took 7m 15s, an estimated $6.24 of agent time.*

## The request

On the page for one run, add a 'Latest activity' section listing the run's 20 most recent events, newest first, each with its time, kind and summary. It refreshes with the page. The database schema is documented in docs/run-store.md; read it. Use only the Python standard library.

## The approved plan (version 1)

# Plan: a 'Latest activity' section on the run page

## What the reader sees

`/runs/<id>`, for a run that was read, ends with a new section after the steps table: the line `<h2>Latest activity</h2>`, then the run's 20 most recent events, newest first, as a table of Time, Kind and Summary. It is the same table the step page already shows under Events, produced by the existing `_render_events`, unchanged:

- no events: `No events recorded`
- events could not be read (table or a column missing, or locked): `Events unavailable: could not read the events table`; the run facts and the steps table are still shown
- the 'Run not found' (404) page and the 'Run unavailable' page are unchanged and have no such section

## Assumptions

1. 'Most recent' and 'newest first' mean highest `events.id` first, not `at`. `docs/run-store.md` guarantees ids only increase, the `events_by_run (run_id, id)` index serves it, and the rest of the viewer orders by id.
2. 'It refreshes with the page' means the section has no mechanism of its own. It is read with the run and its steps on every request, so the existing 5-second reload of a live run refreshes it. The page of an ended run still does not reload itself.
3. The events are those of the whole run, including ones with no step (`step_id` NULL, e.g. `run_state`). Every kind is shown as stored, including kinds the viewer does not know. The payload is not shown, as run-store.md advises.
4. The section goes last on the page. Events do not link to their step.

## `viewer.py` (the only code file changed; standard library only, no new import)

### Data layer

- Add `LATEST_EVENTS = 20  # how many of a run's newest events the run page shows`.
- Add `_EVENT_COLUMNS = 'id, at, kind, summary'` and `_event(row) -> Event` beside `_STEP_COLUMNS` / `_step_summary`. `_event` holds the conversion now written inline in `read_step_page_data` (`Event(row[0], _parse_time(row[1]), row[2] or '', row[3] or '')`). `read_step_page_data` uses both; its results do not change.
- `RunPageData` gains one field, between `steps` and `read_at` (the same position `events` has in `StepPageData`), with no default:
  `events: tuple[Event, ...] | Unreadable  # the newest LATEST_EVENTS of the run, newest first`
  The field order becomes `run_id run steps events read_at`. `live` and `current_step` are untouched.
- Generalise `_read_one_and_its_rows` so it takes any number of rows statements: `_read_one_and_its_rows(one_sql, params, *rows_sql)` returns `(row, rows, ...)`, one `rows` per statement, in order.
  - Still one read-only connection, one `BEGIN`, closed once, using only `execute` and `close` on the connection (the tests' proxy offers nothing else).
  - Connect, `BEGIN` or the one row fails: every item is `UNREADABLE`.
  - No such row: `(None, (), ...)`, and no rows statement is run.
  - Otherwise each rows statement is tried on its own: one that raises `sqlite3.Error` gives `UNREADABLE` for itself only, and the others are still run.
  - Update the docstring to say this.
- `read_run_page_data` becomes `run, steps, events = _read_one_and_its_rows(<run sql>, (run_id,), <steps sql>, f'SELECT {_EVENT_COLUMNS} FROM events WHERE run_id = ? ORDER BY id DESC LIMIT {LATEST_EVENTS}')`. It converts events with `_event` unless `UNREADABLE` and returns `RunPageData(run_id, run, steps, events, read_at)`. Extend its docstring: events are read in the same transaction; unreadable events keep the run and the steps; unreadable steps keep the events.
- `read_step_page_data` calls the helper in its new shape: `step, events = _read_one_and_its_rows(<step sql>, (step_id, run_id), <events sql>)`.

### Presentation layer

- `render_run_page`: unpack the five fields; for a readable run the body is the current three parts plus `'<h2>Latest activity</h2>\n' + _render_events(events)`. It renders the events in the order given and never sorts or cuts them.
- `_render_events`: only its docstring changes ('a step's events' becomes 'events'). The run page has no other element with id `events` or `event-<id>`, so ids stay unique.

### HTTP layer

No change. Update the module docstring's sentence about the run page to mention the latest activity.

## Tests (`tests/test_viewer.py`; the factory allows at most 300 added lines)

Existing tests that must be updated because the hand-off changed:

- The field table: `('RunPageData', 'run_id run steps events read_at')`.
- Every place that builds `RunPageData` positionally: the `render_run` helper (give it `events=()`), the `LIVE_CASES` test and the 404 HTTP test.
- `FAILING_STATEMENTS`, run reader: 'the_run' is now three `UNREADABLE` facts; 'the_steps_keeping_the_run' is now `[run, UNREADABLE, ()]`.
- Every exact-document case of a readable run (`RUN_DOCUMENTS`, the status-label test, `LIVE_RUN_DOCUMENTS`) now ends with the heading line and `NO_EVENTS_LINE`. State that suffix once, in a constant or the helper.

New cases, as rows in the existing tables where one exists:

- `FAILING_STATEMENTS`: statements naming `events` fail for the run reader, giving the run, its steps and `UNREADABLE` events.
- `read_run_page_data` events, one parametrised test over a table of (events inserted, ids expected):
  - none gives `()`
  - ids come back highest first even when `at` says otherwise
  - events of another run are left out
  - an event with `step_id` NULL is included
  - 25 events give exactly the 20 highest ids
  - one separate assertion that all four fields of an `Event` are mapped, with a millisecond timestamp
- An unknown run id runs no statement naming `events` (extend the existing no-statement-on-steps test).
- A missing `events` table keeps the run and steps and gives `UNREADABLE` events; a missing `steps` table still gives readable events. The second proves the rows statements are independent.
- `RUN_DOCUMENTS` / `LIVE_RUN_DOCUMENTS` rows:
  - events present: the heading then the table rows in the order given (the row text is already stated in `FULL_BLOCKS['events']`)
  - events `UNREADABLE`: the heading then the events-unavailable line
  - a live run still has the refresh tag and the section
  - the unreadable-run and not-found documents stay exactly as they are
- Escaping: add an event with markup to the escape-everything-at-once test and raise its count by 2.
- HTTP: nothing new. The existing `PAGES` test already serves `/runs/7` from a database with an event and compares the body with `render_run_page(read_run_page_data(7))`.

## Docs (for the documenter)

- `docs/viewer.md`: in the run page material, say what the section shows, the 20-event limit, ordering by id, the two replacement lines, and that it updates on each load (every 5 seconds while the run is live).
- `CHANGELOG.md`: one 'Added' entry.

## Left out on purpose

- The backlog's context-manager rewrite of all four readers.
- Paging or a 'more' link, payloads, links from an event to its step.
- Splitting `viewer.py` into modules.
- Any change to the step page's output.

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

- The run page of a readable run now ends with a 'Latest activity' section showing the run's 20 newest events, highest id first, each with time, kind and summary, as the request asked.
- The events are read on every request in the same read transaction as the run and its steps, so the section refreshes with the page and with the existing 5-second reload of a live run; no separate mechanism was added.
- Only the standard library is used; viewer.py gains no new import.
- The change follows the approved plan item by item: the LATEST_EVENTS constant, _EVENT_COLUMNS and _event, the new RunPageData.events field in the planned position, the generalised _read_one_and_its_rows where each rows statement fails on its own, and the unchanged _render_events.
- The 'Run not found' and 'Run unavailable' pages have no section, and the step page's output is unchanged.
- Two small departures from the plan are justified: Event moved above RunPageData because the new field's annotation needs it, and the live-run-with-events case is a separate test because the LIVE_RUN_DOCUMENTS table has no events column and its rows already prove the section appears on a live page.
- Layering is kept: the data layer orders and limits, the hand-off type carries the events, and presentation renders them as given, which a test pins with 25 out-of-order events.
- New test cases are tabled: one parametrised reader test over RUN_EVENTS, a parametrised missing-table test, and new rows in the existing RUN_DOCUMENTS and FAILING_STATEMENTS tables.
- The factory's test run shows 416 passed; I did not rerun them.
- Not blocking: the page does not say when a run has more than 20 events; the request did not ask for it and it is recorded in docs/backlog.md and docs/viewer.md.

## Commits it made on main

- `81dc69f` factory: red (run 13)
- `050dfc2` factory: test (run 13)
- `ba328e3` factory: architecture_review (run 13)
- `9acd56c` factory: document (run 13)
