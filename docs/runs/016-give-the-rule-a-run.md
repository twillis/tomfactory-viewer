# Run 16: accepted

*tidy-code, started 2026-11-19 12:15 UTC, took 2m 14s, an estimated $1.02 of agent time.*

## The request

Give the rule 'a run has not ended' one home in viewer.py. It is written four times: as WHERE outcome IS NULL OR outcome = '' in read_page_data, in RunSummary.ended, in RunPageData.live, and as latest.outcome == '' in _render_latest. Keep one definition and have the others use it. Behaviour must not change.

## The approved plan (version 1)

# Plan: one home for the rule "a run has not ended" in `viewer.py`

## What is there now

| Where | Today | Is it a copy? |
|---|---|---|
| `RunSummary.ended` (`viewer.py:68-70`) | `return self.outcome != ""` | The Python definition |
| `_render_latest` (`viewer.py:536`) | `if latest.outcome == "":` | Yes, a second Python copy |
| `read_page_data` (`viewer.py:384-388`) | `WHERE outcome IS NULL OR outcome = ''` inline in the query | Yes, the SQL form, inline |
| `RunPageData.live` (`viewer.py:190`) | `isinstance(self.run, RunDetail) and not self.run.summary.ended` | No: it already delegates to `RunSummary.ended`. Leave it unchanged. |

## Change: `viewer.py` only

No other file is added or changed. No test is changed, added or removed.

### 1. Add the one home, directly above `class LatestRun` (after `db_path`)

```python
# The one rule for whether a run has ended: it has an outcome. Stored, no outcome is
# NULL or ''; the data layer reads both as "". Change the rule here, in both forms.
_NOT_ENDED_SQL = "outcome IS NULL OR outcome = ''"  # a WHERE condition on ``runs``


def _ended(outcome: str) -> bool:
    """Whether a run with this outcome, as read, has ended."""
    return outcome != ""
```

The SQL text must stay exactly `outcome IS NULL OR outcome = ''`, so the query sent to SQLite is byte-for-byte what it is today.

### 2. `LatestRun`: add an `ended` property

```python
    @property
    def ended(self) -> bool:
        """Whether the run has ended: it has an outcome."""
        return _ended(self.outcome)
```

Fields stay `outcome reason`; `test_hand_off_type_has_its_fields_in_order` checks only `_fields`, and a property does not change tuple equality.

### 3. `RunSummary.ended`: use the rule

Body becomes `return _ended(self.outcome)`. Docstring unchanged.

### 4. `RunPageData.live`: no change

It already uses `RunSummary.ended`.

### 5. `read_page_data`: use the SQL form

Replace the comment and query at `viewer.py:384-388` with:

```python
            row = conn.execute(
                f"SELECT id, request FROM runs WHERE {_NOT_ENDED_SQL} "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
```

Delete the comment `# The SQL form of ``RunSummary.ended``: a run without an outcome.`; the constant's name and its home now say it.

### 6. `_render_latest`: use the rule

`if latest.outcome == "":` becomes `if not latest.ended:`. Nothing else in the function changes.

## Assumptions

- **"One definition" means one block holding two forms.** SQLite cannot call the Python predicate, and the SQL form must also cover a stored NULL, which Python never sees because the data layer reads NULL as `""`. So the rule is kept as a Python predicate and a SQL condition side by side, under one comment. The alternatives were rejected: filtering every run in Python changes what is read, and registering a SQLite function with `create_function` adds machinery and may not pass through the tests' connection proxy.
- **`LatestRun` gets an `ended` property** rather than `_render_latest` calling `_ended` directly, so presentation asks the hand-off type, as it already does for `RunSummary`.
- **`docs/backlog.md` line 20** (the suggestion to share one `ended` rule) is resolved by this change. The plan leaves the file alone; striking the entry is for the document step.

## How the tests show it works

Run the existing suite unchanged (`pytest tests/test_viewer.py`); every test must pass as before. The ones that pin each site:

- `RunSummary.ended` and `status`: `test_run_summary_ended_and_status_are_decided_by_the_outcome_not_the_state` (table `RUN_STATUSES`).
- `RunPageData.live` and `current_step`: the parametrised test near `tests/test_viewer.py:1263-1294` (cases `NOT_ENDED`, `STALE`, unreadable and missing runs).
- The SQL form: `test_read_page_data_open_run_is` (table `OPEN_RUNS`, including the NULL outcome, the `''` outcome in `loose_open_run`, and "not a newer ended run"), and `test_read_page_data_keeps_the_count_when_runs_has_no_outcome_or_reason_columns` (still `UNREADABLE`).
- `_render_latest`: the home page render cases using `NOT_ENDED_LINE`, including `a_run_that_has_not_ended_hides_its_reason`, and the end-to-end check at `tests/test_viewer.py:2341`.
- The standard-library-only import check (`tests/test_viewer.py:2485`): no import is added.

Afterwards, a search of `viewer.py` for `outcome != ""`, `outcome == ""` and `outcome IS NULL` must find each only inside the new block.

## How it went

| Step | Attempt | Result |
|------|---------|--------|
| plan | 1 | passed |
| baseline | 1 | passed |
| tidy_code | 1 | passed |
| test | 1 | passed |
| review | 1 | passed |
| finish | 1 | passed |

**Outcome: accepted.** the tests pass and the review found the result matches the request, the plan and the ideals

## The review

Verdict: **accept**

- The rule 'a run has not ended' now has one home at viewer.py:41-48, holding the SQL condition _NOT_ENDED_SQL and the Python predicate _ended under one comment; a search of viewer.py finds no other copy of either form.
- All four sites named in the request use that home: read_page_data interpolates _NOT_ENDED_SQL, RunSummary.ended calls _ended, RunPageData.live still delegates to RunSummary.ended, and _render_latest asks the new LatestRun.ended property.
- Behaviour is unchanged: the SQL text sent to SQLite is byte-for-byte the same, 'not _ended(outcome)' is exactly 'outcome == ""', and the new property does not alter LatestRun's fields or equality.
- The change follows the approved plan in all six steps with no departures; only viewer.py changed, no test was changed, added or removed, and no import was added.
- The factory's test run shows all 434 existing tests passing; I did not re-run them myself.
- No ideal is broken: presentation now asks the hand-off type rather than comparing to an empty string, the addition is about fourteen lines with none spare, and the parametrised-tests ideal does not apply because tests are held still.
- For the document step, not a reason to reject: docs/backlog.md lines 7 and 20 suggest this change and are now resolved, so they should be struck.

## Commits it made on main

- `f26890b` factory: test (run 16)
