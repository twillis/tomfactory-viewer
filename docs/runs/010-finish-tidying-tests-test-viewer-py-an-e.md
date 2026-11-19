# Run 10: accepted

*tidy-tests, started 2026-11-19 04:03 UTC, took 18m 46s, an estimated $10.30 of agent time.*

## The request

Finish tidying tests/test_viewer.py. An earlier tidy cut it from 6,625 lines to 2,336, and a review found five things left undone. 1) These rules now have no test; give each one a single parametrised test with a table of cases: RunSummary.ended and RunSummary.status over the three outcomes and the three states; the steps 'passed' column being 1, 0 or NULL; what lines render_page puts in the body; the Outcome versus State label; and the exact row rendered for one passed step and one failed step. 2) Fold into tables, do not delete: request shortening (including nine words, one word, punctuation and non-ASCII), and the duration tests on RunSummary and StepSummary. 3) Merge or remove these helpers: record_connects, ended_summary, running_summary, running_detail, unfinished_step, request_cell, duration_cell, check_span, failed_check_span, run_head, RUNS_HEAD, DOCUMENT_TAIL. 4) Aim for under 1,500 lines, and say in your summary the line count you reached and what is left if you did not. 5) There is no limit on lines inserted. Do not change viewer.py.

## The approved plan (version 1)

# Plan: finish tidying tests/test_viewer.py

## Scope

- Change one file: `tests/test_viewer.py`. Do not touch `viewer.py`, docs or `.factory.toml`.
- There is no red phase: every test must pass against the unchanged `viewer.py`.
- Keep the file's conventions: the `viewer` fixture (the module is imported per test), `pytest.param(..., id=...)` tables, and `lambda viewer: ...` for table entries that need the module (as `READERS` and `PAGES` do, since tables cannot name `viewer.UNREADABLE` or `viewer.Check` at import time).

## Assumptions

- 'The three outcomes and the three states' means `accepted`, `rejected`, `needs-me` and `planning`, `awaiting_approval`, `working` (the values in `RUNS_DDL`).
- `.factory.toml` sets `test_budget_lines = 300`. The request says there is no limit on lines inserted, so I take the request to override it for this run. Merging helpers moves code, so the diff will show well over 300 inserted lines even though the file shrinks.
- `RUN_7_HEAD` and `RUN_NOT_FOUND_HEAD` are not in the list of helpers to remove, but they are built by `run_head`, so they go with it.
- The two duration test groups fold into one table of cases crossed with the two types, because `RunSummary.duration` and `StepSummary.duration` follow the same rule.

## Part A: the requested changes

### A1. Helpers (request item 3)

Put all helpers in one block at the top, in this order: database builders, connection watching, hand-built hand-offs, expected HTML, fixtures. Merge or remove as follows.

| Old | New |
|---|---|
| `record_connects` | Merged into `wrap_connections(viewer, monkeypatch, after_execute=None)`, which now returns `(calls, proxies)`. It appends `(args, kwargs)` to `calls` before calling the real connect, so a failing connect is still recorded. All callers unpack the pair. |
| `ended_summary`, `running_summary` | One `run_summary(viewer, run_id=7, state='ended', outcome='accepted', request=RUN_REQUEST, created_at=CREATED_DT, ended_at=ENDED_DT)`. A run that has not ended is `run_summary(viewer, **NOT_ENDED)` with `NOT_ENDED = {**RUNNING, 'outcome': ''}`. The 'Fix the bug' default goes; `RUN_REQUEST` is the only default request. |
| `running_detail` | `run_detail(viewer, target=RUN_TARGET, branch=RUN_BRANCH, reason=RUN_REASON, attempt_limit=3, **summary)`, built on `run_summary(viewer, **summary)`. A running detail is `run_detail(viewer, **NOT_ENDED)`. |
| `unfinished_step` | `step_summary(viewer, **UNFINISHED)`. Drop the `'checks': None` key from `UNFINISHED` (it is already `insert_step`'s default) so the one dict serves both `insert_step` and `step_summary`. |
| `request_cell`, `duration_cell` | Removed. Tests assert the whole `run_row(...)`. |
| `check_span`, `failed_check_span` | Removed. The two span strings are written out in the step-row table (A2.5) and in the one remaining test that needs them. |
| `run_head`, `RUNS_HEAD`, `RUN_7_HEAD`, `RUN_NOT_FOUND_HEAD`, `DOCUMENT_TAIL` | One `document(title, heading, body)` that returns the whole expected page: the head lines, the `<h1>` line, `body`, then the two closing lines. Every exact-document assertion uses it, including the home page and the HTTP 404 test. No test spells out the closing lines itself. |
| `count_unavailable_line(path)` | Constant `COUNT_UNAVAILABLE_LINE` with a `{db}` placeholder, filled with `html.escape(str(path))`. |
| `render_runs` | Accepts `viewer.UNREADABLE` as `render_run` already does (wrap in `tuple` only when not unreadable). |

### A2. Five rules that have no test (request item 1)

One parametrised test per rule.

1. **`RunSummary.ended` and `.status`.** Table `RUN_STATUSES` of `(state, outcome, ended, status, label)`:
   - `ended` with `accepted`, `rejected`, `needs-me`: `True`, the outcome, `Outcome`.
   - `planning`, `awaiting_approval`, `working` with outcome `''`: `False`, the state, `State`.
   - The two stale rows from the existing test: `working` + `rejected` gives `True, rejected, Outcome`; `ended` + `''` gives `False, ended, State`.

   The test asserts `run.ended is ended` and `run.status == status`. It replaces `test_run_summary_ended_is_decided_by_the_outcome_not_the_state`.
2. **The steps `passed` column.** Table `(stored, passed)`: `(1, True)`, `(0, False)`, `(None, None)`. Build with `make_run_db(home, runs=[{}], steps=[{'passed': stored}])` and assert `step.passed is passed` on the one step from `read_run_page_data(7)`.
3. **The lines `render_page` puts in the body.** Table `(lambda viewer: PageData, lines)`. The test (with the `home` fixture) asserts `render_page(data) == document('tomfactory viewer', 'Hello from the factory', RUNS_LINK_LINE + lines.format(db=html.escape(str(home / 'factory.db'))))`. Cases:
   - `(0, None)`: count line 0.
   - `(3, None)`: count line 3.
   - `(3, UNREADABLE)`: count line, then latest-unavailable line.
   - `(UNREADABLE, None)`: count-unavailable line only.
   - `(UNREADABLE, UNREADABLE)`: count-unavailable line, then latest-unavailable line.
   - `(3, LatestRun('failed', 'tests red'))`: count, outcome and reason lines.
   - `(3, LatestRun('passed', ''))`: count and outcome lines.
   - `(3, LatestRun('', 'still going'))`: count line and not-ended line.
   - Outcome and reason containing quotes: count line, then both lines with `html.escape`d text.

   This replaces every `render_page` test between the 'count line' banner and the 'a latest run' group, plus `test_render_page_is_otherwise_the_same_document_as_before`. Keep `test_render_page_escapes_html_in_the_database_path` and the does-not-open-the-database test.
4. **Outcome versus State label.** Parametrised over `RUN_STATUSES`. `render_run(viewer, run_detail(viewer, state=state, outcome=outcome))` must equal `document('Run 7 - tomfactory viewer', 'Run 7', run_facts(label=label, status=status, reason=RUN_REASON if ended else None) + NO_STEPS_LINE)`. It replaces `test_render_run_page_decides_outcome_or_state_by_the_outcome_not_the_state` and `..._leaves_out_the_reason_of_a_run_that_has_not_ended`.
5. **The exact row for a step.** Table `(lambda viewer: StepSummary, expected step_row(...))`; the test asserts the row is in `render_run(viewer, run_detail(viewer), [step])` with the default `read_at=ENDED_DT`. Cases:
   - One passed step: id 11, `red`, `test-writer`, one passed check `guard`; the row has result `passed` and the plain check span.
   - One failed step: attempt 2, `passed=False`, checks `guard` passed and `tests` failed; the row has result `failed` and both spans joined by a comma and a space.
   - Unfinished (`**UNFINISHED`): `3m 7s so far`, `not finished`.
   - Finished without `ended_at`: `unknown`, `failed`.
   - Unfinished without `started_at`: `unknown`, `not finished`.
   - Unreadable checks: `unreadable`.

   It replaces the unfinished-row test and the two unknown-duration step tests. Keep `..._shows_other_steps_checks_beside_unreadable_ones` and the order test.

### A3. Fold into tables, delete nothing (request item 2)

- **Request shortening.** One test, table `(request, shown)`, asserting `run_row(7, 'accepted', shown)` is in the page.
  - Existing cases: ten words in full; eleven words cut to ten plus the ellipsis; whitespace only gives an empty cell; newlines and tabs collapse; trailing whitespace adds no ellipsis; the markup case, cut and escaped.
  - New cases:
    - Nine words, shown in full.
    - One word, shown in full.
    - Ten words carrying punctuation, shown in full: `Fix it, now: one-two (three) four. five! six? seven; eight`.
    - A lone dash counting as a word: `a - b c d e f g h i j` gives `a - b c d e f g h i` plus the ellipsis.
    - Non-ASCII in full: `café ✓ naïve 日本語 über`.
    - Non-ASCII cut: `α β γ δ ε ζ η θ ι κ λ` gives the first ten plus the ellipsis.
- **Durations on RunSummary and StepSummary.** One test with two stacked parametrisations.
  - `DURATION_OWNERS` holds two builders taking `(viewer, done, start, end)`: `run_summary(viewer, outcome='accepted' if done else '', created_at=start, ended_at=end)` and `step_summary(viewer, passed=True if done else None, started_at=start, ended_at=end)`.
  - `DURATION_CASES` is `(done, start, end, now, expected)` in seconds after `CREATED_DT`, with `None` for absent:
    - done, 0, 187, now 400 days later: 187.
    - done, 0, 187, now a day before: 187.
    - not done, 0, None, now 7207: 7207.
    - not done with a stray end of 187, now 5: 5.
    - done, 0.123, 187.623: 187.5.
    - done, no start: None.
    - done, no end: None.
    - done, end at -30: 0.
    - not done, now -30: 0.
    - not done, no start: None.
  - It replaces all six RunSummary and all eight StepSummary duration tests.
- **Run rows on /runs.** One table `(run_summary kwargs, run_row arguments)`: default; `NOT_ENDED` gives `3m 7s so far`; no `created_at` gives unknown and unknown; no `ended_at` gives unknown duration; `NOT_ENDED` without `created_at` gives unknown and unknown; markup in the state is escaped. It replaces the running-row test, the three unknown tests and the state-escaping test.
- **Formatted durations.** Keep `DURATIONS`, assert via `run_row`, and add the row `187.4` gives `3m 7s` in place of the whole-seconds test.
- **Exact documents.** One table for `render_runs_page` (one run, no runs, unreadable) and one for `render_run_page` (two steps, no steps, unreadable steps, unreadable run, not found with id 99), each asserting equality with `document(...)`.
- **Run facts.** A small table `(run_detail kwargs, run_facts kwargs)` for the empty reason, the empty branch showing `none`, and the long request shown in full.

## Part B: further folds toward 1,500 (request item 4)

Part A alone lands near 1,950 lines by my estimate. Apply these in order and stop once the file is under 1,500. Each keeps every rule.

1. One banner comment per subject (about 15) instead of about 60 (about -130).
2. The six `_leave_database_missing`-style functions become one dict of id to `lambda home: ...`; `UNREADABLE_DATABASES` and `DATABASE_STATES` are built from it (about -30).
3. The two 'same moment' tests share one helper that opens the WAL holder and commits a given insert right after the first statement matching a predicate (about -40).
4. `NOT_RUN_PAGE_PATHS`, `MALFORMED_TIMES` and `BAD_CHECKS` become plain value lists where the value says what the id said (about -40).
5. The three 'does not open the database' tests become one table of `(render lambda, forbid_db_path)`. Forbid `db_path` for every case except the home page with an unreadable count, which legitimately looks it up (about -30).
6. The three 'statement fails' tests become one table of `(reader, hook, expected facts)` (about -20).
7. `test_read_run_page_data_maps_every_field...` builds its expected steps with `step_summary` (about -25).
8. The two default-port `main` tests become one table that also asserts `fake.closed`; the two `db_path` default tests become one table over unset and empty (about -20).
9. The stored checks cases (empty list, stored order) become one table of `(stored, pairs)` (about -15).

Stop rule: never drop a case or an assertion to reach the number. My estimate after all of Part B is roughly 1,550-1,650 lines, so 1,500 may not be reached. If it is not, say so.

## The builder's summary must state

- The final line count of `tests/test_viewer.py` (`wc -l`).
- If it is 1,500 or more, what is left: the largest remaining blocks (expected: the two DDL strings, the builders, the HTTP path table) and why they were kept.

## How to check the work

- `python -m pytest -q -p no:cacheprovider` passes with `viewer.py` unchanged (`git diff --stat` shows only `tests/test_viewer.py`).
- Searching the test file for `record_connects`, `ended_summary`, `running_summary`, `running_detail`, `unfinished_step`, `request_cell`, `duration_cell`, `check_span`, `run_head`, `RUNS_HEAD` and `DOCUMENT_TAIL` finds nothing.
- `pytest --collect-only -q` shows each of the five A2 tests once per table row: 8, 3, 9, 8 and 6 cases.
- Every test removed in A2, A3 or Part B has its assertion as a row in the table that replaced it. Check this test by test against the starting commit.

## How it went

| Step | Attempt | Result |
|------|---------|--------|
| plan | 1 | passed |
| baseline | 1 | passed |
| tidy_tests | 1 | passed |
| test | 1 | passed |
| review | 1 | passed |
| finish | 1 | passed |

**Outcome: accepted.** the tests pass and the review found the result matches the request, the plan and the ideals

## The review

Verdict: **accept**

- Each of the five untested rules now has one parametrised test with a table of cases, with the counts the plan set: 8, 3, 9, 8 and 6.
- Request shortening and the RunSummary and StepSummary duration tests were folded into tables with no case or assertion lost; the nine-word, one-word, punctuation and non-ASCII cases are present.
- All twelve named helpers are gone, along with RUN_7_HEAD, RUN_NOT_FOUND_HEAD and count_unavailable_line; a search of the file finds none of them.
- The file is 1,830 lines, down from 2,336 but above the 1,500 aim and about 200 above the plan's estimate; the request allows a miss if it is reported, all nine Part B folds were applied, and no case was dropped to chase the number.
- What is left is mostly the 570-line helper block: the two DDL strings, the builders, the expected-HTML constants and the fixtures.
- I was not shown the builder's summary, so I assumed it states the line count and what is left; if it does not, record 1,830 lines and the helper block as the remainder.
- Only tests/test_viewer.py changed, viewer.py is untouched, and the factory reports 324 tests passing.
- Worth a backlog note, not a rejection: FAILING_STATEMENTS picks readers by position (READERS[1].values[0]), so reordering READERS would silently change what it tests.

## Commits it made on main

- `dfffe1a` factory: test (run 10)
