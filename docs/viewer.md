# tomfactory viewer

Four web pages about the factory's runs.

- The home page, at `/`, says "Hello from the factory" and shows how many runs the factory has recorded, which is the number of rows in the `runs` table. Below the count it shows the outcome and reason of the most recent run. It links to the runs page ("All runs").
- The runs page, at `/runs`, lists every run, newest first. See "All runs" below.
- The run page, at `/runs/<id>`, shows one run, a table of its steps and its latest activity, the run's 20 newest events. An unknown id gives a 404 page. Until the run has ended, the page also says which step the run is on now and reloads itself every 5 seconds. See "One run while it is running" and "Latest activity" below.
- The step page, at `/runs/<id>/steps/<step id>`, shows one step in full. See "One step" below.

## Start it

From the repository root:

```
python viewer.py
```

Then open <http://127.0.0.1:8000/>. Press Ctrl+C to stop.

To use another port:

```
python viewer.py --port 9000
```

## Requirements

Python 3.10 or later. There is nothing to install; the app uses only the standard library.

## Where it reads from

`factory.db` in the directory named by `TOMFACTORY_HOME`. If the variable is unset or empty, it uses `~/.tomfactory`.

```
TOMFACTORY_HOME=/path/to/home python viewer.py
```

The database is opened read-only (`mode=ro`). The viewer never creates the file or its directory and never writes to the database.

Every page reads the database afresh on every request, so reloading a page shows the current values. Only the page of a run that has not ended reloads itself, every 5 seconds; the other pages do not refresh themselves.

## Latest run

The latest run is the row in `runs` with the highest `id`. The page reads its `outcome` and `reason` columns and shows one of the following below the count:

- **The run has ended** (its `outcome` is not empty):

  > Latest run outcome: **passed**
  >
  > Latest run reason: all green

  The reason line is left out if `reason` is empty.

- **The run has not ended** (its `outcome` is `NULL` or the empty string):

  > Latest run has not ended yet

  Only the latest row counts. If it has not ended, the page says so even when older runs have ended. Any text in `reason` is not shown until `outcome` is filled in.

- **There are no runs** (the `runs` table has no rows): nothing is added. The count shows 0.

- **The latest run could not be read** (the database, the `runs` table, or either of the `outcome` and `reason` columns is missing):

  > Latest run unavailable: could not read outcome and reason from the runs table

  In the missing-column case the count is still shown above this line. The wording is the same whatever the cause, so it also appears when the file is missing or the database is locked; the line above it tells you whether the count could be read.

The run's id is not shown. Outcome and reason text is HTML-escaped.

The count and the latest run are read on one read-only connection, inside one read transaction, so a page shows a count and a latest run from the same moment.

## When the count is not available

If `factory.db` is missing, is not a SQLite database, or has no `runs` table, the page still loads and shows this in place of the count:

> Run count unavailable: no readable runs table in /path/to/factory.db

The "Latest run unavailable" line appears below it, because the latest run cannot be read either.

The count appears on the next reload once the database is readable.

### To see the unavailable line

Make a database whose `runs` table has no `outcome` or `reason` column, and point the viewer at it:

```
mkdir /tmp/tf-demo
python -c "import sqlite3; c = sqlite3.connect('/tmp/tf-demo/factory.db'); c.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY)'); c.execute('INSERT INTO runs DEFAULT VALUES'); c.commit()"
TOMFACTORY_HOME=/tmp/tf-demo python viewer.py
```

<http://127.0.0.1:8000/> shows "Runs recorded: 1" and then "Latest run unavailable: …". With `TOMFACTORY_HOME` set to an empty directory, it shows both unavailable lines.

## All runs

<http://127.0.0.1:8000/runs> shows a table with one row per run in the `runs` table, highest `id` first. The home page links to it; the runs page has no link back.

| Column | What it shows |
| --- | --- |
| Id | The run's `id`. |
| Status | The run's `outcome` once it has ended (`accepted`, `rejected` or `needs-me`), and its `state` until then (`planning`, `awaiting_approval` or `working`). A run has ended when its `outcome` is not empty, the same rule the home page uses. |
| Request | The first 10 words of `request`, followed by `…` if there were more. Line breaks and runs of spaces become single spaces. |
| Created | `created_at`, in UTC, as `2026-11-18 21:04:05 UTC`. |
| Duration | For an ended run, `ended_at` minus `created_at`. For a run that has not ended, the time since `created_at`, followed by "so far". |

Durations are in whole seconds, starting at the largest unit that is not zero: `42s`, `3m 7s`, `2h 0m 7s`, `1d 0h 3m 7s`.

Status and request text is HTML-escaped.

In place of the table the page shows:

- **The `runs` table has no rows:**

  > No runs recorded yet

- **The runs could not be read** (the database, the `runs` table, or any of the columns `id`, `state`, `outcome`, `request`, `created_at` and `ended_at` is missing, or the database is locked):

  > Runs unavailable: could not read the runs table

  The page still returns 200. The wording is the same whatever the cause.

Things to know:

- **A run whose driver died** but which has not been marked ended is shown with its stored state and a duration that keeps growing. The page does not check whether the driver is alive. See guarantee 6 in [run-store.md](run-store.md).
- **Timestamps** are read only in the form `2026-11-18T21:04:05Z`. A timestamp in any other form (fractional seconds, a numeric offset, no `Z`) shows "unknown" under Created, and the Duration that depends on it is also "unknown". The other rows are not affected.
- **An ended run that ended before it was created**, by its timestamps, shows `0s`, not a negative duration.
- **There is no paging.** Every run is on the one page, and each request reads the full `request` text of every run.
- All rows come from one statement on one read-only connection, so a page shows one moment.

### To see it

With a factory database in place, start the viewer and open <http://127.0.0.1:8000/runs>, or follow "All runs" from the home page. With `TOMFACTORY_HOME` set to an empty directory, the page shows the "Runs unavailable" line.

## One run while it is running

<http://127.0.0.1:8000/runs/7> shows run 7 and a table of its steps. While the run has not ended (its `outcome` is empty, the same rule as on the other pages; `state` is not consulted), the page differs in two ways.

**It reloads itself every 5 seconds.** The page head carries `<meta http-equiv="refresh" content="5">`. There is no JavaScript and no extra HTTP header. Each reload reads the database afresh. The first reload after the run ends comes back without the tag, so the refreshing stops by itself.

**It says which step the run is on now**, in one line between the facts of the run and the steps table:

> Current step: [test](http://127.0.0.1:8000/runs/7/steps/12), 3m 7s so far

The name links to the step's page. The time is the time since the step's `started_at`, in the same form as the Duration column, and is "unknown" when `started_at` cannot be read. When no step is running, the line is instead:

> No step is running now

The page of an ended run has neither the tag nor either line.

Things to know:

- **The current step is the run's newest step row** (highest `id`), if that step has not finished (its `passed` is `NULL`). It is not taken from `runs.current_step`: [run-store.md](run-store.md) does not say how that name relates to the step rows, and a step row has the `started_at` the running time needs. Using the rows also means the line cannot disagree with the steps table below it.
- **"No step is running now"** appears when the newest step has finished, when the run has no steps yet (between steps, or while `awaiting_approval`), and when the steps could not be read. An older unfinished step under a finished newer one is not named.
- **Only a run that was read and has not ended refreshes.** The "run not found" page and the "could not read the runs table" page do not, because the page cannot know that run has not ended. So an open page of a running run that hits one unreadable read (for example, a locked database) stops refreshing and stays stale until you reload it by hand. Nothing on the page says so. This is in `docs/backlog.md`.
- **A run whose driver died** but which has not been marked ended keeps refreshing every 5 seconds, with a step time that keeps growing, until the next `factory` command ends it. The page does not check whether the driver is alive. See guarantee 6 in [run-store.md](run-store.md).
- **A reload is a whole-page reload**, so the browser may lose your scroll position on a run with many steps.
- The home, runs and step pages do not refresh, including the page of a step that is still running.

### To see it

Start the viewer while the factory has a run in progress, open <http://127.0.0.1:8000/runs> and go to that run's page (`/runs/<id>`). The "Current step" time grows by about 5 seconds at each reload, and the line changes as the run moves from step to step. When the run ends, the line disappears and the page stops reloading. To check for the tag from a shell:

```
curl -s http://127.0.0.1:8000/runs/<id> | grep http-equiv
```

It prints the meta line for a run that has not ended and nothing for one that has.

## Latest activity

The page of a run, ended or not, finishes with a "Latest activity" section below the steps table. It lists the run's 20 most recent rows of `events`, newest first, in a table of Time (UTC, as `2026-11-18 21:04:05 UTC`), Kind and Summary. It is the same table the step page shows under Events, in the opposite order.

In place of the table the section shows:

- **The run has no events:**

  > No events recorded

- **The events could not be read** (the `events` table or a column the page reads is missing, or the read failed). The facts of the run and the steps table are still shown:

  > Events unavailable: could not read the events table

The "Run not found" page and the "could not read the runs table" page have no such section.

Things to know:

- **"Most recent" means highest `id`, not latest `at`.** [run-store.md](run-store.md) guarantees that event ids only increase, and the rest of the viewer orders by id. An event whose `at` is out of step with its id is listed by its id.
- **It updates when the page loads, and has no mechanism of its own.** The events are read with the run and its steps on every request, so the 5-second reload of a run that has not ended keeps the section current. The page of an ended run does not reload itself.
- **The events are those of the whole run**, including events that belong to no step (`step_id` is `NULL`, such as `run_state`). Every kind is shown as stored, including kinds the viewer does not know.
- **The page does not say when there are more than 20.** There is no count, no paging and no "more" link. To see every event of a step, open that step's page. This is in `docs/backlog.md`.
- **An event does not name or link to its step**, and its payload is not shown, as [run-store.md](run-store.md) advises.
- **The events and the steps fail separately.** If the `steps` table cannot be read, the events are still shown, and the other way round.
- Times are in whole seconds, and kind and summary text is HTML-escaped. The run, its steps and its events are read on one read-only connection, inside one read transaction, so a page shows one moment.

### To see it

Start the viewer and open the page of any run (`/runs/<id>`); the section is at the bottom. For a run in progress, new events appear at the top of the table within 5 seconds. To count the rows from a shell (at most 20):

```
curl -s http://127.0.0.1:8000/runs/<id> | grep -c '<tr id="event-'
```

## One step

<http://127.0.0.1:8000/runs/7/steps/12> shows step 12 of run 7, under the heading "Step 12 of run 7". Each step name in the steps table on the run page links to that step's page. The step page has no link back to the run.

From top to bottom the page shows:

| Part | What it shows |
| --- | --- |
| Name, Persona, Attempt, Result, Duration | The same facts as the step's row on the run page. Persona is `none` when the step has no persona. Result is `passed`, `failed` or `not finished`. Duration is followed by "so far" while the step is unfinished. |
| Asked | `steps.asked`, the full text, with its line breaks kept. |
| Produced | One entry per key of the `steps.produced` object, in stored order. A value that is text is shown as it is; any other value (a list, a number, an object) is shown as indented JSON. |
| Checks | One row per check in `steps.checks`, in the order the checks ran: name, `passed` or `failed`, and the check's note with its line breaks kept. The note cell is empty when the check has no note. |
| Events | One row per row of `events` for this step, lowest `id` first: time (UTC, as `2026-11-18 21:04:05 UTC`), kind and summary. |

All stored text is HTML-escaped.

In place of a part, the page shows:

- **`produced` is `NULL` or `{}`:**

  > Nothing produced

- **The step has no checks yet** (`checks` is `NULL` or an empty list):

  > No checks recorded

- **The step has no events:**

  > No events recorded

- **A part could not be read.** Each line is specific to its part, and the rest of the page is still shown:

  > Produced unavailable: could not read what the step produced

  when `produced` is not a JSON object;

  > Checks unavailable: could not read the checks of this step

  when `checks` is not a JSON list of checks with a text `name` and a true-or-false `passed`;

  > Events unavailable: could not read the events table

  when the `events` table is missing or lacks a column the page reads.

- **The step itself could not be read** (the database or the `steps` table is missing or locked, or a column is missing). The heading stays, and the page returns 200:

  > Step unavailable: could not read the steps table

### Step not found

The page returns 404, with the heading "Step not found" and the line

> No step with id 99 in run 7

in three cases: there is no step with that id, the step belongs to another run, or there is no run with that id. The message is the same for all three; the page does not say which.

Things to know:

- **Every event of the step is on the one page.** There is no paging, and an agent step writes one event per tool use and per piece of agent text, so a long step makes a long page. This is in `docs/backlog.md`.
- **Event payloads are not shown**, only each event's summary, as [run-store.md](run-store.md) advises. Also not shown: the step's kind, failure, tokens and cost, and a check's `timed_out`.
- **Event times are in whole seconds.** Events in the same second are told apart by their order, which is by `id`. A time not written exactly as `2026-11-18T21:04:05Z` shows "unknown".
- **A check's note that is not text** (missing, `null`, a number) is shown as empty; it does not make the checks unreadable.
- The step and its events are read on one read-only connection, inside one read transaction, so a page shows one moment.

### To see it

With a factory database in place, start the viewer, open <http://127.0.0.1:8000/runs>, go to the page of a run (`/runs/<id>`) and follow a step's name. To see the not-found page, open a step id that does not exist, such as <http://127.0.0.1:8000/runs/1/steps/999999>.

## Limits

- The server listens on `127.0.0.1` only, so it is reachable from the same machine and not from the network. There is no option to change the address.
- Only `GET /`, `GET /runs`, `GET /runs/<id>` and `GET /runs/<id>/steps/<step id>` are served, with or without a query string. An id is a plain number of at most 18 digits with no leading zero. Any other path returns 404, including `/runs/`, `/runs/7/`, `/runs/07`, `/runs/7/steps` and `/runs/7/steps/1/`.
- A database in WAL mode is tested only while another connection has it open. Not tested: a WAL database that nothing else has open, or one in a directory the viewer cannot write to. SQLite may create `factory.db-shm` and `factory.db-wal` beside the database even for a read-only connection, or fail to open it if the directory is not writable; in the second case the page shows the "unavailable" message.

## Tests

```
python -m pytest -q -p no:cacheprovider
```

The tests are in `tests/test_viewer.py` and need pytest.
