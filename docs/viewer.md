# tomfactory viewer

A single web page that says "Hello from the factory" and shows how many runs the factory has recorded, which is the number of rows in the `runs` table. Below the count it shows the outcome and reason of the most recent run.

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

The count and the latest run are read afresh on every request, so reloading the page shows the current values. The page does not refresh itself.

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

- **There is no run to show**: nothing is added. This happens when the table has no rows, and also when the database, the `runs` table, or either of the `outcome` and `reason` columns is missing. The page does not say which. In the missing-column case the count is still shown, so a count above zero with no latest-run line means the columns could not be read.

The run's id is not shown. Outcome and reason text is HTML-escaped.

The count and the latest run are read on two separate connections, so if a run is recorded between the two reads, one page can show a count and a latest run from slightly different moments. Reload to get a consistent page.

## When the count is not available

If `factory.db` is missing, is not a SQLite database, or has no `runs` table, the page still loads and shows this in place of the count:

> Run count unavailable: no readable runs table in /path/to/factory.db

The count appears on the next reload once the database is readable.

## Limits

- The server listens on `127.0.0.1` only, so it is reachable from the same machine and not from the network. There is no option to change the address.
- Only `GET /` is served. Any other path returns 404.
- Not tested: a database in WAL mode. SQLite may create `factory.db-shm` and `factory.db-wal` beside the database even for a read-only connection, or fail to open it if the directory is not writable; in the second case the page shows the "unavailable" message.

## Tests

```
python -m pytest -q -p no:cacheprovider
```

The tests are in `tests/test_viewer.py` and need pytest.
