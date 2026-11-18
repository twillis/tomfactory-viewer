"""Hello-world observability pages for the factory.

Serves two pages. The home page, at ``/``, says hello and shows how many runs
the factory has recorded, plus the outcome and reason of the most recent run.
The runs page, at ``/runs``, lists every run, newest first. Uses only the
Python standard library.

There are three layers: data (``read_*``), presentation (``render_*``) and HTTP
(``_Handler``). The data layer hands the presentation layer one ``PageData`` or
``RunsPageData``; the presentation layer never reads the database or the clock.
"""

import argparse
import html
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple


def db_path() -> Path:
    """Return the path of the factory database, read from the environment now."""
    home = os.environ.get("TOMFACTORY_HOME")
    if not home:
        return Path("~/.tomfactory").expanduser() / "factory.db"
    return Path(home) / "factory.db"


class LatestRun(NamedTuple):
    """What the page knows about the most recent run."""

    outcome: str  # "" until the run has ended
    reason: str  # "" until the run has ended


class Unreadable(Enum):
    """Marks a fact that could not be read from the database."""

    UNREADABLE = "unreadable"


UNREADABLE = Unreadable.UNREADABLE


class PageData(NamedTuple):
    """Everything the page shows: what the data layer hands to the presentation layer."""

    count: int | Unreadable  # number of rows in ``runs``
    latest: LatestRun | None | Unreadable  # None only when ``runs`` has no rows


class RunSummary(NamedTuple):
    """What the runs page knows about one run, and the rules that follow from it."""

    id: int
    state: str
    outcome: str  # "" until the run has ended
    request: str  # the full text
    created_at: datetime | None  # UTC-aware; None if it could not be parsed
    ended_at: datetime | None  # UTC-aware; None if absent or not parseable

    @property
    def ended(self) -> bool:
        """Whether the run has ended: it has an outcome."""
        return self.outcome != ""

    @property
    def status(self) -> str:
        """The outcome once the run has ended, its state until then."""
        return self.outcome if self.ended else self.state

    def duration(self, now: datetime) -> timedelta | None:
        """How long the run took, or has been running as of ``now``; None if unknown."""
        if self.created_at is None:
            return None
        if self.ended:
            if self.ended_at is None:
                return None
            span = self.ended_at - self.created_at
        else:
            span = now - self.created_at
        return max(span, timedelta(0))


class RunsPageData(NamedTuple):
    """Everything the runs page shows: the data layer's hand-off to presentation."""

    runs: tuple[RunSummary, ...] | Unreadable  # in the order to show them
    read_at: datetime  # UTC-aware; when the read was made


def _connect() -> sqlite3.Connection:
    """Open the factory database read-only, so it is never created or changed."""
    return sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)


def _parse_time(value) -> datetime | None:
    """Return the UTC-aware time a stored timestamp names, or None if it names none."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def read_page_data() -> PageData:
    """Read everything the page shows, over one read-only connection.

    The database is opened read-only, so it is never created or changed. Both facts
    are read inside one read transaction, so they describe the same moment. A fact
    that could not be read is ``UNREADABLE``; ``latest`` is None only when the
    ``runs`` table was read and has no rows.
    """
    try:
        conn = _connect()
    except sqlite3.Error:
        return PageData(UNREADABLE, UNREADABLE)
    try:
        try:
            conn.execute("BEGIN")
        except sqlite3.Error:
            return PageData(UNREADABLE, UNREADABLE)

        count: int | Unreadable
        try:
            count = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        except sqlite3.Error:
            count = UNREADABLE

        latest: LatestRun | None | Unreadable
        try:
            row = conn.execute(
                "SELECT outcome, reason FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            latest = None if row is None else LatestRun(row[0] or "", row[1] or "")
        except sqlite3.Error:
            latest = UNREADABLE

        return PageData(count, latest)
    finally:
        conn.close()


def read_runs_page_data() -> RunsPageData:
    """Read every run, highest id first, over one read-only connection.

    One statement is run, so the rows describe one moment. If the database, the
    ``runs`` table or any column the page needs could not be read, ``runs`` is
    ``UNREADABLE``; an empty table gives an empty tuple.
    """
    read_at = datetime.now(timezone.utc)
    try:
        conn = _connect()
    except sqlite3.Error:
        return RunsPageData(UNREADABLE, read_at)
    try:
        rows = conn.execute(
            "SELECT id, state, outcome, request, created_at, ended_at "
            "FROM runs ORDER BY id DESC"
        ).fetchall()
    except sqlite3.Error:
        return RunsPageData(UNREADABLE, read_at)
    finally:
        conn.close()
    runs = tuple(
        RunSummary(
            row[0],
            row[1] or "",
            row[2] or "",
            row[3] or "",
            _parse_time(row[4]),
            _parse_time(row[5]),
        )
        for row in rows
    )
    return RunsPageData(runs, read_at)


def _document(title: str, body: str) -> str:
    """Return a full HTML document; ``body`` is lines each ending in a newline."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{body}"
        "</body>\n"
        "</html>\n"
    )


def _render_latest(latest: LatestRun | None | Unreadable) -> str:
    """Return the page lines for the latest run, each ending in a newline."""
    if latest is UNREADABLE:
        return (
            '<p id="latest-run-unavailable">Latest run unavailable: '
            "could not read outcome and reason from the runs table</p>\n"
        )
    if latest is None:
        return ""
    if latest.outcome == "":
        return '<p id="latest-run-not-ended">Latest run has not ended yet</p>\n'
    lines = (
        '<p>Latest run outcome: <strong id="latest-run-outcome">'
        f"{html.escape(latest.outcome)}</strong></p>\n"
    )
    if latest.reason != "":
        lines += (
            '<p>Latest run reason: <span id="latest-run-reason">'
            f"{html.escape(latest.reason)}</span></p>\n"
        )
    return lines


def render_page(data: PageData) -> str:
    """Return the full HTML page for the given page data."""
    count, latest = data
    if count is UNREADABLE:
        status = (
            '<p id="run-count-unavailable">Run count unavailable: '
            f"no readable runs table in {html.escape(str(db_path()))}</p>"
        )
    else:
        status = f'<p>Runs recorded: <strong id="run-count">{count}</strong></p>'
    return _document(
        "tomfactory viewer",
        "<h1>Hello from the factory</h1>\n"
        '<p><a id="runs-link" href="/runs">All runs</a></p>\n'
        f"{status}\n"
        f"{_render_latest(latest)}",
    )


REQUEST_WORDS = 10  # how many words of a request the runs page shows


def _first_words(text: str) -> str:
    """Return the first ``REQUEST_WORDS`` words of ``text``, with … if there were more."""
    words = text.split()
    shown = " ".join(words[:REQUEST_WORDS])
    return shown + "…" if len(words) > REQUEST_WORDS else shown


def _format_time(dt: datetime | None) -> str:
    return "unknown" if dt is None else dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_duration(td: timedelta | None) -> str:
    """Return whole seconds as e.g. ``42s``, ``3m 7s``, ``2h 0m 7s`` or ``1d 0h 3m 7s``."""
    if td is None:
        return "unknown"
    minutes, seconds = divmod(max(int(td.total_seconds()), 0), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [(days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s")]
    while len(parts) > 1 and parts[0][0] == 0:
        parts.pop(0)
    return " ".join(f"{amount}{unit}" for amount, unit in parts)


def _render_run_row(run: RunSummary, read_at: datetime) -> str:
    """Return the table line for one run, ending in a newline."""
    span = run.duration(read_at)
    duration = _format_duration(span)
    if span is not None and not run.ended:
        duration += " so far"
    return (
        f'<tr id="run-{run.id}">'
        f'<td class="run-id">{run.id}</td>'
        f'<td class="run-status">{html.escape(run.status)}</td>'
        f'<td class="run-request">{html.escape(_first_words(run.request))}</td>'
        f'<td class="run-created">{_format_time(run.created_at)}</td>'
        f'<td class="run-duration">{duration}</td>'
        "</tr>\n"
    )


def render_runs_page(data: RunsPageData) -> str:
    """Return the full HTML runs page for the given data, rows in the order given."""
    runs, read_at = data
    if runs is UNREADABLE:
        listing = (
            '<p id="runs-unavailable">Runs unavailable: '
            "could not read the runs table</p>\n"
        )
    elif not runs:
        listing = '<p id="no-runs">No runs recorded yet</p>\n'
    else:
        listing = (
            '<table id="runs">\n'
            "<thead>\n"
            "<tr><th>Id</th><th>Status</th><th>Request</th><th>Created</th>"
            "<th>Duration</th></tr>\n"
            "</thead>\n"
            "<tbody>\n"
            + "".join(_render_run_row(run, read_at) for run in runs)
            + "</tbody>\n"
            "</table>\n"
        )
    return _document("Runs - tomfactory viewer", "<h1>Runs</h1>\n" + listing)


# Every path served, and how its page is made. Any other path is a 404.
_PAGES = {
    "/": lambda: render_page(read_page_data()),
    "/runs": lambda: render_runs_page(read_runs_page_data()),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        page = _PAGES.get(self.path.split("?", 1)[0])
        if page is None:
            self.send_error(404)
            return
        body = page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """Return a bound server that has not started serving."""
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Hello-world factory viewer.")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    args = parser.parse_args(argv)

    server = make_server(port=args.port)
    print(f"Serving on http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
