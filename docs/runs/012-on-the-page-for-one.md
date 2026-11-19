# Run 12: accepted

*change, started 2026-11-19 10:38 UTC, took 6m 44s, an estimated $5.54 of agent time.*

## The request

On the page for one run (/runs/<id>), while the run has not ended, show which step it is on now and how long that step has been running, and have the page refresh itself every 5 seconds. A page for an ended run must not refresh. The database schema is documented in docs/run-store.md; read it. Use only the Python standard library.

## The approved plan (version 1)

# Plan: live run page (current step, its running time, self-refresh)

One file changes: `viewer.py`. No new file, no new SQL, no new import. `read_run_page_data` is untouched, because everything needed is already in `RunPageData`.

## Assumptions

1. **The step a run is on now is its newest step row (highest `id`), if that step has not finished** (`passed` is `NULL`, the existing `StepSummary.finished` rule). It is taken from the `steps` rows the page already reads, not from `runs.current_step`. Reasons: `docs/run-store.md` guarantees a step row exists from the moment the step starts and gives it `started_at`, which the running time needs; it does not say how `runs.current_step` (a bare name) relates to step rows. One source also means the line cannot disagree with the steps table below it. If the newest step has finished, or there are no steps (between steps, or `awaiting_approval`), the page says no step is running.
2. **The refresh is `<meta http-equiv="refresh" content="5">` in the page head.** No JavaScript and no new HTTP header, so the HTTP layer does not change.
3. **Only a run that was read and has not ended refreshes.** An unreadable run and a run not found do not refresh, because the page cannot know they have not ended. Cost: a live page that hits one unreadable read stops refreshing until reloaded by hand.
4. **Ended keeps its existing meaning**, `RunSummary.ended` (outcome not empty), not `state`.
5. Only `/runs/<id>` changes. The home, runs and step pages do not refresh.

## Changes to `viewer.py`

### Hand-off type (the interface between data and presentation)

Add two properties to `RunPageData`. Its fields stay the same.

- `live -> bool`: true when `run` is a `RunDetail` and `run.summary.ended` is false. False for `None` and `UNREADABLE`.
- `current_step -> StepSummary | None`: `steps[-1]` when `live`, `steps` is a non-empty tuple and `steps[-1].finished` is false; otherwise `None` (including `steps is UNREADABLE`).

### Presentation layer

- Add the constant `REFRESH_SECONDS = 5`.
- `_document(title, body, refresh: int | None = None)`: when `refresh` is given, add the line `<meta http-equiv="refresh" content="{refresh}">\n` directly after the `<meta charset="utf-8">` line. Other callers are unchanged.
- Add `_step_link(run_id, step) -> str`, returning `<a href="/runs/{run_id}/steps/{step.id}">{escaped name}</a>`. `_render_step_row` uses it; its output is unchanged.
- Add `_render_current_step(data: RunPageData) -> str`:
  - not `data.live`: `""`
  - `data.current_step` is `None`: `<p id="no-current-step">No step is running now</p>\n`
  - otherwise: `<p id="current-step">Current step: {_step_link(...)}, {_step_duration(step, data.read_at)}</p>\n`, for example `Current step: test, 3m 7s so far`, or `Current step: test, unknown` when `started_at` could not be parsed.
- `render_run_page`: for a readable run the body becomes `_render_run_facts(run) + _render_current_step(data) + _render_steps(...)`, and the final `_document` call passes `refresh=REFRESH_SECONDS if data.live else None`. The not-found and unreadable branches are unchanged.
- Update the module docstring to mention the run page's current step and refresh.

Result: the page of an ended run is byte-for-byte what it is today. A page left open on a running run reloads every 5 seconds; the first reload after the run ends has no meta tag, so refreshing stops by itself.

## Tests (`tests/test_viewer.py`, for the test writer)

`.factory.toml` allows 300 added lines of tests per run, so use tables.

Helpers: give `document()` an optional `refresh=None` that adds the meta line. Add constants for the two new lines.

1. **One parametrised test for `RunPageData.live` and `.current_step`**, on hand-built data. Columns: run, steps, expected `live`, expected current step id or `None`.

   | Run | Steps | `live` | Current step |
   | --- | --- | --- | --- |
   | not ended | newest unfinished | true | that step |
   | not ended | two unfinished | true | the newer |
   | not ended | older unfinished, newest finished | true | `None` |
   | not ended | all finished | true | `None` |
   | not ended | `()` | true | `None` |
   | not ended | `UNREADABLE` | true | `None` |
   | ended | newest unfinished | false | `None` |
   | state `working` with an outcome | any | false | `None` |
   | `UNREADABLE` | `UNREADABLE` | false | `None` |
   | `None` | `()` | false | `None` |

2. **Extend `RUN_DOCUMENTS`** (exact whole documents; add a `refresh` column or pass it through `document`):
   - not ended, with an unfinished step read 187 s after it started: the meta line, and `run_facts(label="State", status="working", reason=None)` + the `current-step` line with link `/runs/7/steps/12` and `3m 7s so far` + the steps table;
   - not ended, no steps: the meta line, and the facts + the `no-current-step` line + `NO_STEPS_LINE`;
   - not ended, unfinished step with `started_at=None`: `Current step: …, unknown`;
   - not ended, step name `<u>&"'`: escaped in the line.

   The existing ended, unreadable and not-found cases already prove those pages have no meta line and no new paragraph, because they compare whole documents.
3. **Update `test_render_run_page_labels_the_status_and_gives_the_reason_by_the_outcome`**: when `ended` is false, the expected document has `refresh=5` and the `no-current-step` line before `NO_STEPS_LINE`. This is the only existing test whose expectation changes.
4. **One parametrised HTTP test** on `GET /runs/7`: (run kwargs, expects refresh) = (`RUNNING`, true), (`{}`, false). Assert the meta line is or is not in the body; for the running case, with an unfinished step inserted, also assert `id="current-step"` and the step link are present. Use containment, not equality, because the duration depends on the clock.
5. The existing exact-document tests for the home, runs and step pages already show those pages gain no meta line. `test_viewer_imports_only_the_standard_library` and `test_hand_off_type_has_its_fields_in_order` must pass unchanged.

Check with `python -m pytest -q -p no:cacheprovider`.

## Documentation (for the documenter)

- `docs/viewer.md`: replace "The pages do not refresh themselves" with the new behaviour; give the run page a short section covering the two lines, the 5-second refresh, when it stops, and assumptions 1 and 3. Note that a run whose driver died but is not marked ended keeps refreshing (guarantee 6 in `docs/run-store.md`), and that event times aside, the page still reads the database afresh on each reload.
- `CHANGELOG.md`: one entry under Added.

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

- The run page of a run that has not ended now shows the current step with a link and its running time (or 'No step is running now') and carries a 5-second meta refresh; ended, not-found and unreadable pages get neither, and the ended page is byte-for-byte unchanged.
- viewer.py follows the approved plan exactly, adds no import, and uses the schema as docs/run-store.md defines it (unfinished means passed is NULL, ended means an outcome is set).
- The tests depart from the plan in two small ways without losing coverage: the live cases are in a separate table (LIVE_RUN_DOCUMENTS) instead of extending RUN_DOCUMENTS, and the HTTP test is one test covering an ended and a running run instead of a two-row parametrised test.
- All 402 tests pass as run by the factory; I did not rerun them.
- For later, not a reason to reject: current_step relies on steps being oldest first, which holds today because read_run_page_data orders by id, but the field's comment does not promise it; this is already recorded as the backlog warning and should be fixed before the steps table is ever reordered.

## Commits it made on main

- `e8466a8` factory: red (run 12)
- `999f0f9` factory: test (run 12)
- `ab87e41` factory: architecture_review (run 12)
- `46b1506` factory: document (run 12)
