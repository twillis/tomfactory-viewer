# Run 7: accepted

*change, started 2026-11-18 19:24 UTC, took 11m 17s, an estimated $8.08 of agent time.*

## The request

Add a page at /runs listing every run, newest first: its id, its state or (once ended) its outcome, the first words of the request, when it was created, and how long it took or has been running. Link to it from the home page. The database schema is documented in docs/run-store.md in this codebase; read it. Use only the Python standard library.

## The approved plan (version 1)

# Plan: a page at /runs listing every run

## Scope

One file changes: `viewer.py`. Tests go in `tests/test_viewer.py` (test writer); `docs/viewer.md` and `CHANGELOG.md` are for the documenter. No new modules and no new dependencies: an existing test rejects any import in `viewer.py` that is not standard library, which a sibling module would fail. The three layers stay as named functions and types inside `viewer.py`, as now: data (`read_*`), presentation (`render_*`), HTTP (`_Handler`).

## Assumptions

- **Newest first** means highest `id` first, the same rule the home page uses for the latest run.
- **Ended** means `outcome` is non-empty, the home page's existing rule; the schema guarantees this equals `state = 'ended'`.
- **First words of the request** means the first 10 whitespace-separated words joined by single spaces, with `…` appended only if there were more.
- Times are shown in UTC.
- A run whose driver died but is not yet marked ended is shown with its stored state and a growing duration. `docs/run-store.md` guarantee 6 allows this.
- Only `/runs` is served (with or without a query string); `/runs/` stays 404.
- No pagination: the request says every run.
- No link back from `/runs` to home: not requested.

## Data layer (`viewer.py`)

1. Add `_connect()`, returning `sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)`. Use it in `read_page_data` in place of the inline call, with no other change there.
2. Add `_parse_time(value) -> datetime | None`. It returns `datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)`, or `None` if `value` is not a `str` or does not match. Do not use `fromisoformat`: it rejects `Z` on Python 3.10.
3. Add `class RunSummary(NamedTuple)` with fields, in this order:
   - `id: int`
   - `state: str`
   - `outcome: str` (`""` until ended)
   - `request: str` (the full text)
   - `created_at: datetime | None` (UTC-aware; `None` if not parseable)
   - `ended_at: datetime | None`

   and these members, which are the only place the rules are stated:
   - property `ended -> bool`: `self.outcome != ""`.
   - property `status -> str`: `self.outcome` if `self.ended`, else `self.state`.
   - method `duration(now: datetime) -> timedelta | None`: `None` if `created_at` is `None`. If ended: `ended_at - created_at`, or `None` if `ended_at` is `None`. If not ended: `now - created_at`. A negative result becomes `timedelta(0)`.
4. Add `class RunsPageData(NamedTuple)` with `runs: tuple[RunSummary, ...] | Unreadable` and `read_at: datetime` (UTC-aware, taken when the read was made). This is the whole interface between data and presentation for the page.
5. Add `read_runs_page_data() -> RunsPageData`:
   - Set `read_at = datetime.now(timezone.utc)`.
   - Open one connection with `_connect()`.
   - Run exactly one statement: `SELECT id, state, outcome, request, created_at, ended_at FROM runs ORDER BY id DESC`.
   - Build one `RunSummary` per row: `state`, `outcome` and `request` use `value or ""`; both times go through `_parse_time`.
   - Close the connection in `finally`.
   - Any `sqlite3.Error` from connecting, executing or fetching gives `RunsPageData(UNREADABLE, read_at)`.
   - An empty table gives `runs == ()`.

## Presentation layer (`viewer.py`)

Both functions below are pure: no database, environment or clock.

1. Add constant `REQUEST_WORDS = 10`.
2. Add helpers:
   - `_first_words(text) -> str`: per the assumption above, using `text.split()`.
   - `_format_time(dt) -> str`: `dt.strftime("%Y-%m-%d %H:%M:%S UTC")`, or `unknown` for `None`.
   - `_format_duration(td) -> str`: whole seconds, as `42s`, `3m 7s`, `2h 0m 7s` or `1d 0h 3m 7s`. Start at the largest non-zero unit and show every smaller unit. `0s` for zero, `unknown` for `None`.
3. Add `render_runs_page(data: RunsPageData) -> str`. It takes exactly one argument. Same document head as the home page, but `<title>Runs - tomfactory viewer</title>`. The body is `<h1>Runs</h1>\n` followed by one of:
   - runs is `UNREADABLE`: `<p id="runs-unavailable">Runs unavailable: could not read the runs table</p>\n`
   - runs is `()`: `<p id="no-runs">No runs recorded yet</p>\n`
   - otherwise a table, one line per element:
     ```
     <table id="runs">
     <thead>
     <tr><th>Id</th><th>Status</th><th>Request</th><th>Created</th><th>Duration</th></tr>
     </thead>
     <tbody>
     <tr id="run-7"><td class="run-id">7</td><td class="run-status">working</td><td class="run-request">Add a page at /runs listing every run, newest first: its…</td><td class="run-created">2026-11-18 21:04:05 UTC</td><td class="run-duration">3m 7s so far</td></tr>
     </tbody>
     </table>
     ```
     - Rows are in the order given in `data.runs`; the presentation does not sort.
     - Status is `run.status`.
     - Duration is `_format_duration(run.duration(data.read_at))`, with ` so far` appended when the run has not ended and the duration is not `None`.
     - Status and request text are passed through `html.escape`.

   The page ends `</body>\n</html>\n`. Share the document head and tail between the two pages with a small `_document(title, body)` helper if that leaves `render_page`'s output byte-identical apart from the link below.
4. In `render_page`, add the line `<p><a id="runs-link" href="/runs">All runs</a></p>\n` immediately after the `<h1>` line and before the count line. It must go there: existing tests pin everything from the count line to `</html>`.

## HTTP layer (`viewer.py`)

- Replace the single path check in `_Handler.do_GET` with a module-level mapping from path to a function returning the page text:
  - `"/"` → `render_page(read_page_data())`
  - `"/runs"` → `render_runs_page(read_runs_page_data())`
- Strip the query string as now.
- A path not in the mapping gets 404 without opening the database.
- Response headers are unchanged.
- Update the module docstring to mention the second page.

## How tests can show it works

Tests build a database with the full `runs` DDL from `docs/run-store.md`.

**Data**
- Rows come back highest id first, even when inserted out of order.
- Every field is mapped; a NULL `outcome` becomes `""` and a NULL `ended_at` becomes `None`.
- Timestamps parse to UTC-aware datetimes; a malformed one becomes `None`.
- An empty table gives `()`.
- A missing file, a non-SQLite file, a missing table or a missing column gives `UNREADABLE`.
- Each call opens exactly one `mode=ro` connection and closes it once.
- The database file's bytes are unchanged, and a missing file is not created.
- `read_at` lies between the times just before and just after the call.

**Rules**
- `RunSummary.ended`, `status` and `duration` for: an ended run, a running run, a missing `created_at`, an ended run with a missing `ended_at`, and a negative span.

**Presentation**
- With a hand-built `RunsPageData` and a fixed `read_at`, the exact `<tr>` line for an ended run and for a running run (` so far`).
- Row order equals input order.
- A 10-word request has no `…`; an 11-word request is cut to 10 words plus `…`; newlines in the request collapse to spaces.
- Markup in the request and status is escaped.
- The `no-runs` and `runs-unavailable` lines appear, each with no `<table`.
- Each duration format, and `unknown`.
- `render_runs_page` works with `sqlite3.connect` patched to raise.

**Home**
- `render_page` output contains the `runs-link` line between the `<h1>` and the count line.
- All existing tests still pass.

**HTTP**
- `GET /runs` is 200, `text/html; charset=utf-8`, with a correct `Content-Length`.
- `GET /runs` with one run contains that run's row.
- `/runs?x=1` is 200.
- `/runs/` and `/runs/7` are 404.
- A run inserted between two requests appears on the second.
- `/runs` with no database is 200 with `runs-unavailable`.
- `/` contains `href="/runs"`.

## For the documenter

- `docs/viewer.md`: add a section on `/runs`, and correct "Only `GET /` is served".
- `CHANGELOG.md`: add an entry.

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

- The /runs page lists every run, highest id first, showing id, outcome once ended or state until then, the first 10 words of the request, the UTC created time and the duration (with 'so far' for a run that has not ended), and the home page links to it, as the request asked.
- The columns read and the timestamp format parsed match docs/run-store.md, and viewer.py imports only the standard library.
- The change follows the approved plan item by item; the only addition is a private row-rendering helper inside the presentation layer, which changes no interface or output.
- The layers stay separate: the data layer hands presentation one RunsPageData value including the read time, render_runs_page reads no database, environment or clock, the ended/status/duration rules are stated once on RunSummary, and the HTTP layer only maps paths to pages and returns 404 for unknown paths without opening the database.
- The home page's output is unchanged apart from the new link line, and all 376 tests pass, covering every case the plan listed.
- The home page's render_page still calls db_path() for its unavailable message; that crossing predates this change and is already in the backlog, so it is not a reason to reject here.

## Commits it made on main

- `4c345d5` factory: red (run 7)
- `7b0ceb3` factory: test (run 7)
- `5ad1653` factory: architecture_review (run 7)
- `63a039f` factory: document (run 7)
