# Run 15: needs-me

*tidy-code, started 2026-11-19 12:06 UTC, took 5m 34s, an estimated $3.17 of agent time.*

> **Operator's note.** The planner was not told this was a tidy that holds the tests still, and planned new tests the run could not write. The factory was fixed to tell the planner and reviewer the kind of run; run 16 is the same request, accepted first time.

## The request

Give the rule 'a run has not ended' one home in viewer.py. It is written four times: as WHERE outcome IS NULL OR outcome = '' in read_page_data, in RunSummary.ended, in RunPageData.live, and as latest.outcome == '' in _render_latest. Keep one definition and have the others use it. Behaviour must not change.

## The approved plan (version 1)

# Plan: one home for the rule "a run has not ended"

## What is there now (`viewer.py`)

| Place | Line | How the rule is written |
|---|---|---|
| `RunSummary.ended` | 70 | `self.outcome != ""` |
| `_render_latest` | 536 | `latest.outcome == ""` |
| `read_page_data`, open-run query | 386 | `WHERE outcome IS NULL OR outcome = ''` |
| `RunPageData.live` | 190 | `not self.run.summary.ended` |

`RunPageData.live` already delegates to `RunSummary.ended`, so it is **not changed**. There are three written copies to reduce to one, not four.

## Changes: `viewer.py` only

1. **The one definition.** Add a module-level function just above `class LatestRun`:
   ```python
   def _ended(outcome: str) -> bool:
       """Whether a run has ended: it has an outcome. The one home of this rule."""
       return outcome != ""
   ```
   It takes the outcome as the data layer already hands it over: `""` for a stored NULL, via the existing `row[n] or ""`.

2. **`RunSummary.ended`**: the body becomes `return _ended(self.outcome)`. The docstring stays.

3. **`LatestRun`**: add an `ended` property with the same body and docstring as `RunSummary.ended`. Its fields stay `outcome reason`, so `test_hand_off_type_has_its_fields_in_order` still passes.

4. **`_render_latest`**: `if latest.outcome == "":` becomes `if not latest.ended:`. Nothing else in the function changes.

5. **`read_page_data`, the open-run read**: drop the SQL predicate and pick the run in Python with `_ended`. Replace the `try` body, and delete the comment `# The SQL form of RunSummary.ended ...`:
   ```python
   try:
       rows = conn.execute("SELECT id, outcome, request FROM runs ORDER BY id DESC")
       row = next((row for row in rows if not _ended(row[1] or "")), None)
   except sqlite3.Error:
       open_run = UNREADABLE
   else:
       open_run = None if row is None else OpenRun(
           row[0], row[2] or "", _read_step_now(conn, row[0])
       )
   ```
   - The `next(...)` must stay inside the `try`, because rows are fetched lazily.
   - `request` is now `row[2]`.
   - It is still one connection and one transaction, using only `execute`, which is all the tests' `ConnectionProxy` offers.

6. **`RunPageData.live`** and every other user of `.ended` (`status`, `duration`, `_render_runs` rows, the run page) are untouched.

After this, the literal comparison of an outcome to `""` appears once in the file, in `_ended`, and `outcome IS NULL` appears nowhere.

### Why the SQL copy is replaced, not shared

SQL cannot call the Python rule. Registering it with `create_function` would use a connection method the data layer is not meant to use: `ConnectionProxy` allows only `execute` and `close`. A shared SQL constant would still be a second definition. Picking the row in Python is the only option that leaves one definition.

## Assumptions and costs

- **Edge case.** A stored outcome that is falsy but neither NULL nor `''` (an empty blob, or `0` in a column with no type affinity) is treated as ended by today's SQL, but as not ended by every Python reader, because of `or ""`. After the change the open run agrees with the Python readers. The documented schema and the factory never store such values; no existing test covers it.
- **Cost.** The open-run read no longer has `LIMIT 1`. It walks runs newest first until the first one that has not ended, and walks all of them when every run has ended. The runs page already reads every run, so this is acceptable for a local viewer.
- **Name.** `_ended` is private; tests reach the rule through `LatestRun.ended` and `RunSummary.ended`.

## How tests can show it works: `tests/test_viewer.py`

**Unchanged behaviour.** Every existing test must pass without edits. The ones that matter most:
- `OPEN_RUNS` / `test_read_page_data_open_run_is`
- `test_read_page_data_keeps_the_count_when_runs_has_no_outcome_or_reason_columns` (open run still `UNREADABLE`)
- `test_read_page_data_count_and_latest_run_come_from_the_same_moment`
- `PAGE_BODIES`, `RUN_STATUSES`, `LIVE_CASES`
- the every-reader tests (one connection, closed once, no writes)

**New tests, parametrised, failing before the change:**

1. *The outcome decides `ended` on both hand-off types.* One test over a table of `(outcome, ended)` with rows `("", False)`, `("accepted", True)`, `("rejected", True)`. It asserts `viewer.LatestRun(outcome, "").ended is ended` and `run_summary(viewer, outcome=outcome).ended is ended`. It is red today because `LatestRun` has no `ended`.

2. *Every reader agrees on whether a stored run has ended.* One test over stored outcomes `None`, `""` and `"accepted"`. Build a one-run database with that outcome: use `make_run_db` with `RUNNING` or the defaults where the documented DDL allows the value, and a loose table like `loose_open_run` for `""`. Then assert all four agree with the expected `ended`:
   - `read_page_data().latest.ended`
   - `read_page_data().open_run is None`
   - `read_runs_page_data().runs[0].ended`
   - `not read_run_page_data(id).live`

3. *The rule is written once.* One small test reads `viewer.py`'s source, as `test_viewer_imports_only_the_standard_library` does. It asserts `"outcome IS NULL"` is absent, and that an outcome is compared with `""` in exactly one function, `_ended`. An `ast` walk is preferred over a text search: find `Compare` nodes with an `outcome` name or attribute on one side and `""` on the other.

## How it went

| Step | Attempt | Result |
|------|---------|--------|
| plan | 1 | passed |
| baseline | 1 | passed |
| tidy_code | 1 | passed |
| test | 1 | passed |
| review | 1 | check |
| tidy_code | 2 | passed |
| test | 2 | passed |
| review | 2 | check |
| tidy_code | 3 | passed |
| test | 3 | passed |
| review | 3 | check |

**Outcome: needs-me.** the tidy_code step has run 3 times without success; the last failure, in review: The viewer.py change is correct and follows plan steps 1 to 6: the rule is defined once in _ended, and RunSummary.ended, the new LatestRun.ended, _render_latest and the open-run read in read_page_data all use it. No change to viewer.py is needed.; The approved plan required three new parametrised tests in tests/test_viewer.py and none of them was written; the 434 passing tests are only the existing suite. Nothing in the change gives a reason for leaving them out.; Add the planned test over (outcome, ended) rows ("", False), ("accepted", True), ("rejected", True) asserting both viewer.LatestRun(outcome, "").ended and run_summary(viewer, outcome=outcome).ended. Today no test reads LatestRun.ended directly.; Add the planned test over stored outcomes None, "" and "accepted" asserting that read_page_data().latest.ended, read_page_data().open_run is None, read_runs_page_data().runs[0].ended and not read_run_page_data(id).live all agree.; Add the planned test that reads viewer.py's source and asserts that "outcome IS NULL" is absent and that an outcome is compared with "" in exactly one function, _ended, preferably by an ast walk. Without it nothing stops the rule being written a second time later.

## The review

Verdict: **reject**

- The viewer.py change is correct and follows plan steps 1 to 6: the rule is defined once in _ended, and RunSummary.ended, the new LatestRun.ended, _render_latest and the open-run read in read_page_data all use it. No change to viewer.py is needed.
- The approved plan required three new parametrised tests in tests/test_viewer.py and none of them was written; the 434 passing tests are only the existing suite. Nothing in the change gives a reason for leaving them out.
- Add the planned test over (outcome, ended) rows ("", False), ("accepted", True), ("rejected", True) asserting both viewer.LatestRun(outcome, "").ended and run_summary(viewer, outcome=outcome).ended. Today no test reads LatestRun.ended directly.
- Add the planned test over stored outcomes None, "" and "accepted" asserting that read_page_data().latest.ended, read_page_data().open_run is None, read_runs_page_data().runs[0].ended and not read_run_page_data(id).live all agree.
- Add the planned test that reads viewer.py's source and asserts that "outcome IS NULL" is absent and that an outcome is compared with "" in exactly one function, _ended, preferably by an ast walk. Without it nothing stops the rule being written a second time later.

Nothing from this run was merged.
