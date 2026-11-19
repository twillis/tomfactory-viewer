# Run 14: accepted

*change, started 2026-11-19 10:52 UTC, took 8m 12s, an estimated $6.19 of agent time.*

## The request

On the home page, if a run has not ended, show it first: its id linking to its page, the first words of its request, and the step it is on now. On /runs, mark the run that has not ended so it stands out. The database schema is documented in docs/run-store.md; read it. Use only the Python standard library.

## The approved plan (version 1)

# Plan: show the run that has not ended, on the home page and on /runs

## What changes

Two files change, `viewer.py` and `tests/test_viewer.py`; the documenter then updates `docs/viewer.md` and `CHANGELOG.md`. No new files and no new imports (standard library only, and `test_viewer_imports_only_the_standard_library` keeps holding).

"Not ended" keeps the viewer's one existing rule: the run's `outcome` is empty (`NULL` or `""`); `state` is not consulted. `docs/run-store.md` (index `one_open_run`) guarantees at most one such run.

## Assumptions

1. **"Show it first"** means the first thing under the `<h1>` on the home page, above the "All runs" link, the count and the latest-run lines. Those existing lines stay as they are, including "Latest run has not ended yet".
2. **"The step it is on now"** is the rule the run page already uses: the run's newest `steps` row (highest `id`) if it has not finished (`passed` is `NULL`). It is not `runs.current_step`, for the reason `docs/viewer.md` already gives. So the home page and the run page cannot disagree.
3. **"The first words"** are what `/runs` shows: `_first_words` (10 words, then `…`).
4. **"Stands out"** on `/runs` means the status text of that row is wrapped in `<mark>`, which browsers highlight without CSS. The app has no stylesheet and none is added.
5. The home page does not start refreshing itself, and shows no step duration (neither was asked for; `PageData` has no clock).

## `viewer.py`

### Data layer

- Add a module-level function that states the current-step rule once:
  ```python
  def _step_now(newest: StepSummary | None) -> StepSummary | None:
      """The step a run that has not ended is on now: its newest step, if unfinished."""
      return None if newest is None or newest.finished else newest
  ```
  `RunPageData.current_step` keeps its guards and returns `_step_now(self.steps[-1])`. Its behaviour does not change.
- Add a hand-off type, placed after `StepSummary`:
  ```python
  class OpenRun(NamedTuple):
      """What the home page knows about the run that has not ended."""
      id: int
      request: str  # the full text
      step: StepSummary | None  # the step it is on now; None if none is running or the steps could not be read
  ```
- `PageData` gains a third field with a default, so existing two-argument constructions keep working. Because it refers to `OpenRun`, move `PageData` below `OpenRun` or quote the annotation.
  ```python
  open_run: OpenRun | None | Unreadable = None  # None only when no run is without an outcome
  ```
- `read_page_data`, on the same connection and inside the same `BEGIN` as the count and the latest run, reads a third fact:
  1. `SELECT id, request FROM runs WHERE outcome IS NULL OR outcome = '' ORDER BY id DESC LIMIT 1`. This is the SQL form of `RunSummary.ended`; say so in a comment.
  2. On `sqlite3.Error`, `open_run` is `UNREADABLE`. With no row, it is `None`.
  3. With a row, run `f"SELECT {_STEP_COLUMNS} FROM steps WHERE run_id = ? ORDER BY id DESC LIMIT 1"` with the run's id. `step` is `_step_now(_step_summary(row))`, or `None` if there is no row or the statement raised `sqlite3.Error`. This matches the run page, where unreadable steps give "No step is running now".
  4. `open_run = OpenRun(id, request or "", step)`.
  - Both early returns (connect failed, `BEGIN` failed) become `PageData(UNREADABLE, UNREADABLE, UNREADABLE)`.
  - Update the docstring: three facts, one moment.

### Presentation layer

- Add `_render_open_run(open_run) -> str`, lines each ending in a newline:
  - `None` gives `""`.
  - `UNREADABLE` gives `<p id="open-run-unavailable">Open run unavailable: could not read the runs table</p>\n`.
  - An `OpenRun` gives one line, with the request as `html.escape(_first_words(request))`:
    - with a step: `<p id="open-run">Run <a href="/runs/7">7</a> has not ended: Add a page for one run. Current step: <a href="/runs/7/steps/12">test</a></p>\n`. The step link is the existing `_step_link(open_run.id, open_run.step)`.
    - without a step: the same line ending `. No step is running now</p>\n`.
- `render_page` unpacks three fields and puts `_render_open_run(open_run)` between the `<h1>` line and the runs-link line. `_first_words`, `REQUEST_WORDS` and `_step_link` are defined further down the module, which is fine at call time; do not copy them.
- `_render_run_row`: the status cell becomes `<td class="run-status"><mark>{status}</mark></td>` when `not run.ended`, and is unchanged otherwise. Compute the escaped status once and wrap it conditionally. Nothing else in the row changes.

### HTTP layer

No change. The routes already call `render_page(read_page_data())` and `render_runs_page(read_runs_page_data())`.

### Module docstring

Say that the home page shows the run that has not ended first, and that `/runs` marks it.

## `tests/test_viewer.py` (test writer; budget 300 added lines)

Existing tests to adjust:

- `test_hand_off_type_has_its_fields_in_order`: `PageData` is `count latest open_run`; add `("OpenRun", "id request step")`.
- `test_read_page_data_latest_is_the_not_ended_row_when_older_rows_have_ended`: the expected value gains `viewer.OpenRun(2, RUN_REQUEST, None)`.
- The `read_page_data` tests on loose tables (`make_db` with only `id`, or `id, outcome, reason`) have no `request` column, so `open_run` is `UNREADABLE` there. These are the stored-text table, the two missing-column tests and the same-moment test. Compare `data[:2]` with the old pair, and in the missing-columns test also assert `data.open_run is viewer.UNREADABLE` while the count is kept.
- `RUN_ROWS`: the three `NOT_ENDED` cases expect the status `<mark>working</mark>` (and `<mark>` around the escaped nasty state).

The every-reader tests (all facts unreadable, one read-only connection closed once, files untouched, `PAGES` over HTTP) already cover the new read through `facts()` and need no change.

New tests, each one table of cases:

1. **`read_page_data` open run**, built with `make_run_db(home, runs=..., steps=...)` and asserting `read_page_data().open_run`:

   | Case | Expected |
   | --- | --- |
   | no runs | `None` |
   | only ended runs | `None` |
   | open run 7, no steps | `OpenRun(7, RUN_REQUEST, None)` |
   | open run, steps 11 finished and 12 unfinished | step id 12, equal to `step_summary(...)` built by hand |
   | open run, step 11 unfinished and newest step 12 finished | step `None` |
   | open run 7 with no steps, ended run 6 with an unfinished step | step `None` |
   | open run 7 with a higher-id ended run 8 | run 7 |
   | open run, `steps_table=False` | `OpenRun(7, RUN_REQUEST, None)` |

   One more case on a loose table `runs (id, outcome TEXT, request TEXT)` with `outcome = ''` and `request` `NULL` expects `OpenRun(1, "", None)`.

2. **`render_page` open-run line**: a table of `(open_run, line)` asserting the whole page equals `document("tomfactory viewer", "Hello from the factory", line + RUNS_LINK_LINE + COUNT_3)` for `PageData(3, None, open_run)`:
   - with a step
   - without a step
   - `UNREADABLE`
   - a 13-word request (`LONG_REQUEST`) cut to `TEN_WORDS + "…"`
   - markup in the request and in the step name escaped (`NASTY_NAME`)

   The existing `PAGE_BODIES` already prove that `None` adds nothing.

3. **`render_runs_page` mark**: a page of one ended and one not-ended run contains `<mark>` exactly once, inside the row of the run that has not ended.

4. **Rendering without the database**: add one `RENDERS_WITHOUT_THE_DATABASE` case for the home page with an `OpenRun`.

5. **HTTP**: with ended run 6 and running run 7 that has unfinished step 12 (the fixture of `test_get_run_page_refreshes_...`):
   - `GET /` contains the `open-run` line once, before `RUNS_LINK_LINE`, and equals `render_page(read_page_data())`.
   - `GET /runs` contains `<mark>working</mark>` once.
   - The `open-run` link's `href`, `/runs/7`, answers 200.

## Docs (documenter)

- `docs/viewer.md`:
  - A section on the home page's open-run line: its three forms, the two rules it shares with the other pages, and that the home page still does not refresh.
  - A line under "All runs" on the `<mark>`.
- `CHANGELOG.md`: an Added entry, and a Changed entry saying `PageData` has a third field, `open_run`, with default `None`.

## Not in this plan

Backlog refactors (a shared read helper, `sqlite3.Row`, splitting modules), a home-page refresh, CSS and dead-driver detection. None was asked for.

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

- The home page shows the run that has not ended as the first line under the heading, with its id linking to its run page, the first 10 words of its request, and its current step linking to the step page or 'No step is running now'.
- On /runs the status of the run that has not ended is wrapped in <mark>, and ended runs are unchanged.
- viewer.py gains no imports, so it still uses only the Python standard library, and the queries use only columns documented in docs/run-store.md.
- The change follows the approved plan item by item; the only departure is that the step read is its own small function, _read_step_now, which changes no behaviour.
- The data, presentation and HTTP layers stay separate, with the new OpenRun type as the explicit hand-off, and existing helpers are reused rather than copied.
- The new tests are tables of cases, and the factory reports 434 passing; I did not re-run them.
- Not covered by tests, for later: a steps table that exists but cannot be read, the open-run read being in the same moment as the count and latest run, and a database where an empty outcome and state disagree.
- The open run's step being None both when no step is running and when the steps could not be read was approved in the plan and is recorded in docs/backlog.md; it is the first thing to fix next.

## Commits it made on main

- `201841b` factory: red (run 14)
- `599e6e7` factory: test (run 14)
- `3589785` factory: architecture_review (run 14)
- `5b6b8ed` factory: document (run 14)
