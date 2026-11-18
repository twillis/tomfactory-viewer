# Backlog

Findings from architecture reviews. Each entry is one line: what to do, in which file, and whether it is a suggestion or a warning.

## From run 5 (show the most recent run's outcome and reason)

- **Warning** — `viewer.py`: `_fetch_one` returns `None` both for "no row" and for "could not read", so `latest_run()` cannot tell an empty table from a renamed or missing `outcome`/`reason` column and the page silently shows nothing on schema drift; have the data layer distinguish "unreadable" from "no runs" so the page can say which. (Ideal: explicit interfaces.)
- **Warning** — `viewer.py`: `_Handler.do_GET` calls `count_runs()` and `latest_run()` on two separate connections, so one page can show a count and a latest run from different moments, and every new fact adds another connection and another `render_page` parameter; read one page-data value (count plus latest) in a single read-only connection and pass that one value to `render_page`. (Ideal: explicit interfaces between layers.)
- **Suggestion** — `viewer.py`: the rule "a run has ended when its outcome is non-empty" lives in `_render_latest` and in comments on `LatestRun`, so any second consumer must re-derive it; give `LatestRun` an `ended` property and have the presentation ask that instead of comparing to `""`. (Ideal: well-defined layers.)
- **Suggestion** — `viewer.py`: `render_page` calls `db_path()` to build the "count unavailable" message, so the presentation layer reads the environment and cannot be rendered from its arguments alone; pass the path (or the whole unavailable message) in from the data layer. (Ideal: well-defined layers.)
- **Suggestion** — `tests/test_viewer.py`: the not-ended, empty-reason and escaping rules are each asserted at three levels (`latest_run`, `render_page`, HTTP), so a change to one rule must be made in three sections; keep the rule cases at the `render_page` level and let the HTTP tests assert only that the body equals `render_page(count_runs(), latest_run())` plus freshness between requests. (Ideal: absorb change over time.)
