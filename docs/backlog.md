# Backlog

Findings from architecture reviews. Each entry is one line: what to do, in which file, and whether it is a suggestion or a warning.

## From run 5 (show the most recent run's outcome and reason)

- **Suggestion** — `viewer.py`: the rule "a run has ended when its outcome is non-empty" lives in `_render_latest` and in comments on `LatestRun`, so any second consumer must re-derive it; give `LatestRun` an `ended` property and have the presentation ask that instead of comparing to `""`. (Ideal: well-defined layers.)
- **Suggestion** — `viewer.py`: `render_page` calls `db_path()` to build the "count unavailable" message, so the presentation layer reads the environment and cannot be rendered from its arguments alone; pass the path (or the whole unavailable message) in from the data layer. (Ideal: well-defined layers.)
- **Suggestion** — `tests/test_viewer.py`: the not-ended, empty-reason and escaping rules are each asserted at three levels (`read_page_data`, `render_page`, HTTP), so a change to one rule must be made in three sections; keep the rule cases at the `render_page` level and let the HTTP tests assert only that the body equals `render_page(read_page_data())` plus freshness between requests. (Ideal: absorb change over time.)
