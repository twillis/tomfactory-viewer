# Backlog

Findings from architecture reviews. Each entry is one line: what to do, in which file, and whether it is a suggestion or a warning.

## From run 5 (show the most recent run's outcome and reason)

- **Suggestion** — `viewer.py`: the rule "a run has ended when its outcome is non-empty" lives in `_render_latest` and in comments on `LatestRun`, so any second consumer must re-derive it; give `LatestRun` an `ended` property and have the presentation ask that instead of comparing to `""`. (Ideal: well-defined layers.)
- **Suggestion** — `viewer.py`: `render_page` calls `db_path()` to build the "count unavailable" message, so the presentation layer reads the environment and cannot be rendered from its arguments alone; pass the path (or the whole unavailable message) in from the data layer. (Ideal: well-defined layers.)
- **Suggestion** — `tests/test_viewer.py`: the not-ended, empty-reason and escaping rules are each asserted at three levels (`read_page_data`, `render_page`, HTTP), so a change to one rule must be made in three sections; keep the rule cases at the `render_page` level and let the HTTP tests assert only that the body equals `render_page(read_page_data())` plus freshness between requests. (Ideal: absorb change over time.)

## From run 6 (one page-data value, read once, that says what could not be read)

- **Suggestion** — `viewer.py`: `UNREADABLE` carries no cause, so `read_page_data` turns every `sqlite3.Error` (missing file, missing table, missing column, a busy or locked database) into the same marker and `_render_latest` and `render_page` each hard-code a guessed cause ("could not read outcome and reason from the runs table" is shown even when the file does not exist); have the data layer return the reason with the marker (for example a small `Unreadable(reason)` value holding the SQLite message and the path) and have the presentation print it, which also settles the `db_path()` suggestion above. (Ideal: explicit interfaces between layers.)
- **Suggestion** — `viewer.py`: `read_page_data` writes "run one statement, convert the row, or give `UNREADABLE` on `sqlite3.Error`" out in full for each fact, which is cheaper than a helper at two facts but means a third fact copies the block and its union type again; when the next fact is added, move the pattern into one helper that takes the connection, the SQL and a row converter, and leave it as it is until then. (Ideal: absorb change over time.)
