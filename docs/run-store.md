> Copied from the factory (`tomfactory/specs/001-near-loop-factory/contracts/run-store.md`) on 2026-11-18.
> This is the database the viewer reads. The viewer opens it read-only and never writes to it.
> Sections about the factory's own gateway and rules describe the writer, not the viewer.

# Contract: Run Store

The run store is everything the factory keeps: one SQLite file, `factory.db`, and a folder of per-run files, both in the factory's home directory (`~/.tomfactory` or `TOMFACTORY_HOME`). The factory is the only writer. The observability app and any other reader open the database read-only.

Inside the factory, **all persistence goes through one gateway** in `factory/store.py` (constitution S3). The gateway has five operations, and each takes a table:

| Operation | Returns |
|-----------|---------|
| `insert(table, row)` | the stored row |
| `get(table, key)` | one row, by its primary key |
| `query(table, where, order, limit)` | rows |
| `update(table, where, changes)` | the rows it changed |
| `delete(table, where)` | how many it removed |

It learns each table's columns from the database's own catalog, so a new table needs its DDL and nothing else: no class and no methods. `gateway.files` owns every per-run file. No other module issues SQL or opens a file under the home directory.

The database schema is the contract between the factory and the observability app. Changing it is a breaking change to the app. `tests/test_store.py` exercises it.

*Revised 2026-11-18: the first version had a repo class and a data class per table. The operator asked for a gateway like bttr's; see `changes/2026-11-18-store-gateway.md` and research.md R19.*

## Opening it

- The factory opens the database in WAL mode, so readers never block the writer and the writer never blocks readers.
- Readers must open it read-only and must not hold long transactions.
- The schema carries a version stamp (`PRAGMA user_version`, currently 4). The factory brings older homes up to date itself; a reader only needs to know the current shape, below.

## Tables

```sql
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request         TEXT    NOT NULL,
    target          TEXT    NOT NULL,
    base_commit     TEXT    NOT NULL,
    branch          TEXT,
    workspace       TEXT,
    state           TEXT    NOT NULL CHECK (state IN ('planning','awaiting_approval','working','ended')),
    current_step    TEXT,
    outcome         TEXT    CHECK (outcome IN ('accepted','rejected','needs-me')),
    reason          TEXT,
    ideals          TEXT    NOT NULL DEFAULT '',
    attempt_limit   INTEGER NOT NULL DEFAULT 3,
    driver_pid      INTEGER,
    driver_started  TEXT,
    child_pgid      INTEGER,
    created_at      TEXT    NOT NULL,
    ended_at        TEXT,
    run_kind        TEXT    NOT NULL DEFAULT 'change',   -- change | tidy-tests | tidy-code
    CHECK ((state = 'ended') = (outcome IS NOT NULL))
);
CREATE UNIQUE INDEX one_open_run ON runs ((state != 'ended')) WHERE state != 'ended';
CREATE TABLE plans (
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    version           INTEGER NOT NULL,
    text              TEXT    NOT NULL,
    ideals_addressed  JSON TEXT NOT NULL,
    conflicts         JSON TEXT NOT NULL,
    comment           TEXT,
    decision          TEXT    NOT NULL CHECK (decision IN ('pending','approved','revised','abandoned')),
    created_at        TEXT    NOT NULL,
    PRIMARY KEY (run_id, version)
);
DROP INDEX IF EXISTS one_approved_plan;     -- a run may now be approved more than once: spec, then plan
CREATE TABLE steps (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    name           TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('agent','code')),
    persona        TEXT,
    attempt        INTEGER NOT NULL,
    asked          TEXT    NOT NULL,
    produced       JSON TEXT,
    checks         JSON TEXT,
    passed         BOOLEAN,
    failure        TEXT    CHECK (failure IN ('timeout','error','guard','malformed','check','question')),
    started_at     TEXT    NOT NULL,
    ended_at       TEXT,
    tokens         JSON TEXT,
    cost_estimate  REAL
);
CREATE TABLE events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    step_id   INTEGER REFERENCES steps(id),
    at        TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    summary   TEXT    NOT NULL,
    payload   JSON TEXT NOT NULL
);
CREATE TABLE questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    step         TEXT    NOT NULL,
    asked_by     TEXT,
    question     TEXT    NOT NULL,
    answer       TEXT,
    answered_by  TEXT    CHECK (answered_by IN ('lead','operator')),
    asked_at     TEXT    NOT NULL,
    answered_at  TEXT
);
CREATE INDEX events_by_run ON events (run_id, id);
CREATE INDEX steps_by_run  ON steps  (run_id, id);
```

A column typed `JSON TEXT` holds JSON text; the gateway hands it back parsed. A column typed `BOOLEAN` holds 1 or 0; the gateway hands it back as true or false. To any other reader they are text and a number.

All timestamps are UTC in ISO 8601 form **with milliseconds**, for example `2026-11-18T21:04:05.123Z`. *(Corrected 2026-11-18: this line first gave an example without milliseconds, and a viewer built from it could not read the real values.)*

### What the JSON columns hold

*(Added 2026-11-18, after a planner building the viewer had to stop and ask.)*

| Column | Shape | Example |
|--------|-------|---------|
| `steps.checks` | A list, in the order the checks ran. Each item is an object with `name` (text), `passed` (true or false), `note` (text; why it failed, or test output; often empty when it passed) and `timed_out` (true or false). A check failed when `passed` is false. Empty (`NULL`) until the step finishes. | `[{"name": "tests_pass", "passed": false, "note": "1 failed…", "timed_out": false}]` |
| `steps.produced` | An object. For an agent step, the envelope it replied with, whose keys depend on the step (see the workflow contract); if it gave none, `{"text": "…"}`. For a code step that ran the codebase's tests, `{"output": "…", "contained": {"network": false}}`: what the tests printed, and that they ran contained, with or without the network; otherwise `{}`. `NULL` until the step finishes. | `{"summary": "added greet", "files_changed": ["hello.py"]}` |
| `steps.tokens` | An object with `input` and `output` (numbers), for agent steps; `NULL` for code steps. | `{"input": 25416, "output": 2935}` |
| `plans.ideals_addressed`, `plans.conflicts` | Lists of text. | `["keeps the layers apart"]` |
| `events.payload` | An object whose keys depend on the event's kind. A reader should show `summary` and treat the payload as detail. | `{"name": "tests_pass", "passed": true, "note": "", "timed_out": false}` |

`steps.passed` is `1`, `0`, or `NULL` while the step is running. `steps.failure` is `NULL` when it passed; `question` means the step asked a question instead of finishing (see the `questions` table).

## Rows

The gateway returns rows as plain mappings from column name to value (constitution S5: data, with its types and constraints coming from the catalog and the schema's own `CHECK`s, not from a class per table). `row["state"]` and `row.state` are the same thing. A row is JSON as it stands.

## Conditions

Which rows an operation applies to is said with values, never with SQL text:

- a mapping means "all of these are equal": `{"run_id": 7, "kind": "check"}`; a value of `None` means "is empty"
- `eq`, `ne`, `gt`, `lt`, `is_null`, `not_null` make single conditions
- a list combines several: `[{"run_id": 7}, gt("id", 120)]`

Table and column names are checked against the catalog before anything is run. `update` and `delete` refuse to run with no conditions.

## Where the rules are

The gateway holds no rules about runs, plans or steps. Those are business rules and live in the control layer, in `factory/records.py`, each stated once and done through the five operations:

| Rule | Function |
|------|----------|
| A run's branch is `factory/<id>-<slug>`. The slug is the request's first five words, lower-cased, every run of characters other than `a–z` and `0–9` replaced by `-`, trimmed of `-`, cut to 40 characters; `run` if that leaves nothing | `new_run`, `slug` |
| Changing a run's state writes a `run_state` event | `set_state`, `end` |
| A run ends once: `state`, `outcome`, `reason` and `ended_at` together, and `driver_pid`, `driver_started` and `child_pgid` cleared | `end` |
| Only the fields that move as a run is carried on may be changed afterwards | `move` |
| Plans are numbered 1, 2, 3 …; a decision is made on the current one | `add_plan`, `decide`, `current_plan` |
| A step's attempt number is one more than the earlier records with the same run and name; a step finishes once | `start_step`, `finish_step`, `attempts` |
| The follow query | `events_after` |

Two rules are also held by the database itself, as a backstop: at most one run that has not ended, and at most one approved plan per run. Whether to refuse a second request is decided in `factory/operations.py`, where the operator sees a readable reason.

## The one query for following a run

```sql
SELECT id, step_id, at, kind, summary, payload
FROM events
WHERE run_id = :run AND id > :last_seen
ORDER BY id
LIMIT 500;
```

A live view runs it every second or so with the last id it saw. A history view runs it from zero. They are the same query.

## Event kinds

| Kind | When | Written by |
|------|------|------------|
| `run_state` | The run changed state | `records.set_state`, `records.end` |
| `step_start`, `step_end` | A step began or finished | the driver |
| `agent_text` | The agent wrote text | the driver, from the adapter |
| `tool_call` | The agent used a tool; one event per use | the driver, from the adapter |
| `check` | A check ran; the payload has its name, result and note | the driver |
| `denied` | The agent program refused something the agent tried; the payload has the action and path | the driver, from the adapter |
| `guard` | A forbidden change was found and undone; the payload lists the files | the driver |
| `commit` | The factory committed to the run's branch | the driver |
| `question`, `answer` | A step asked a question; the lead or the operator answered it | `records.ask`, `records.answer` |
| `approved`, `set_aside` | The operator approved at a wait (the payload carries the commit); a build was set aside on a side branch | `operations.approve`, the driver |
| `notify` | A notification was sent, or failed to send | the end-of-run and wait routines |
| `error` | Something went wrong in the factory itself | the driver |

## Guarantees to readers

1. **Rows appear as things happen.** A step row exists from the moment the step starts; `ended_at`, `passed` and `checks` are filled in when it finishes.
2. **Rows are never deleted or rewritten**, with two exceptions: a `steps` row is completed once when its step ends, and a `runs` row is updated as the run moves on.
3. **`events.id` only increases**, so the cursor query never misses or repeats an event.
4. **Event kinds may be added.** A reader must ignore kinds it does not know.
5. **A run that is `ended` never changes again.**
6. **A run can be dead without yet being marked ended.** If the driver was killed, the row stays `planning` or `working` until the next `factory` command notices and ends it as needs-me, "interrupted". A reader that wants to be exact must treat a `planning` or `working` run as stopped when no process with `driver_pid` and `driver_started` exists. The observability app's first versions may simply show the state as stored.

## Per-run files (`gateway.files`)

| Path under the home directory | What it is | Written through |
|-------------------------------|------------|-----------------|
| `ideals.md` | The operator's ideals | the operator's editor |
| `runs/<id>/driver.log` | The driver's own output for that run | `files.driver_log(run_id)` |
| `runs/<id>/steps/<step-id>.jsonl` | The agent program's raw lines for that step, exactly as received | `files.raw_sink(run_id, step_id)` |
| `runs/<id>/steps/<step-id>.stderr` | Anything the step's process wrote to standard error | `files.stderr_file(run_id, step_id)` |
| `runs/<id>/displaced/` | Changed versions of the factory's own files, copied here before they are put back, so nothing an operator edited mid-run is lost | `files.displaced_dir(run_id)` |
| `runs/<id>/snapshot/` | A copy of `personas/`, `prompts/`, the factory's package folder and `ideals.md` taken when the run starts, used to restore them if a step changes them | `files.snapshot_dir(run_id)` |
| `workspaces/<id>/` | The run's checkout of the target codebase | `factory/workspace.py`, at the path `files.workspace_path(run_id)` gives |

The raw files are the record of last resort. Everything a reader needs is in the database.
