# Run 17: accepted

*tidy-code, started 2026-11-19 13:30 UTC, took 1m 38s, an estimated $0.74 of agent time.*

## The request

In viewer.py the texts 'Current step: <link>' and 'No step is running now' are written out in both _render_current_step and _render_open_run. Give each one home and have both functions use it. Behaviour must not change.

## The approved plan (version 1)

# Plan: one home for the current-step phrase in `viewer.py`

## What changes

One file: `viewer.py`. No test, doc or other file changes.

### 1. Add `_current_step_phrase` (presentation layer, directly after `_step_link`, about line 702)

```python
def _current_step_phrase(run_id: int, step: StepSummary | None) -> str:
    """Return the words saying which step a run is on, or that none is running."""
    if step is None:
        return "No step is running now"
    return f"Current step: {_step_link(run_id, step)}"
```

It returns the bare phrase only: no `<p>` wrapper, no id, no duration, no trailing full stop or newline. After this change it is the only place in `viewer.py` where either literal appears.

### 2. `_render_open_run` (about line 564)

Replace the four-line `if open_run.step is None: ... else: ...` block with:

```python
step = _current_step_phrase(open_run.id, open_run.step)
```

The `None` and `UNREADABLE` early returns and the final `<p id="open-run">…` return stay exactly as they are. The helper is defined later in the file than this function, which is fine: it is looked up at call time, as `_step_link` and `_first_words` already are here.

### 3. `_render_current_step` (about line 704)

Keep the `if not data.live: return ""` guard. Replace the rest of the body with:

```python
step = data.current_step
phrase = _current_step_phrase(data.run_id, step)
if step is None:
    return f'<p id="no-current-step">{phrase}</p>\n'
return f'<p id="current-step">{phrase}, {_step_duration(step, data.read_at)}</p>\n'
```

The `step is None` branch stays because the two cases use different element ids and only one has a duration; those belong to this caller, not to the phrase.

## Behaviour

The output is byte-for-byte the same on both pages:
- Home page: `… has not ended: <request>. Current step: <a …>name</a></p>` or `… . No step is running now</p>`.
- Run page: `<p id="current-step">Current step: <a …>name</a>, <duration></p>` or `<p id="no-current-step">No step is running now</p>`.

## How to tell it works

- Run the existing suite unchanged (`pytest tests/test_viewer.py`); every test must pass. The existing cases already pin both phrasings on both pages (`STEP_12_LINK`, `NO_CURRENT_STEP_LINE`, the `current-step` line template near line 496, and `open_run_line(step="No step is running now")` near line 1119).
- `grep -n "Current step: \|No step is running now" viewer.py` shows each literal exactly once, both inside `_current_step_phrase`.
- No test is added, changed or removed.

## Assumptions

- "Give each one home" is read as one small function holding both phrases, because they are two branches of one rule (step or no step). This is also the shape the backlog entry in `docs/backlog.md` (line 60) proposes. Two separate constants would leave the branch duplicated in both callers.
- The backlog entry in `docs/backlog.md` that this request resolves is left untouched, because this run is told to plan only changes to the code. Removing that entry is for the documentation step or the operator.

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

- The texts 'Current step: <link>' and 'No step is running now' now each appear exactly once in viewer.py, both inside the new _current_step_phrase, and both _render_open_run and _render_current_step call it.
- The output is byte-for-byte unchanged on the home page and the run page: the <p> wrappers, element ids, duration and trailing newline stay in the callers exactly as before, and the existing tests that pin all four forms pass (434 passed).
- The change follows the approved plan exactly: same helper signature, docstring and placement after _step_link, the same edits to the two callers, and only viewer.py was touched with no test added, changed or removed.
- No ideal is broken: the helper sits in the presentation layer with a narrow interface, the change is a net reduction that removes a duplicated rule, and the parametrised-tests ideal does not apply because tests are held still.
- Follow-up, not a reason to reject: the backlog entry at docs/backlog.md line 60 that this change resolves is still present and should be removed in the documentation step.

## Commits it made on main

- `c9b765d` factory: test (run 17)
