"""Hello-world observability page for the factory.

Serves one page that says hello and shows how many runs the factory has
recorded, plus the outcome and reason of the most recent run. Uses only the
Python standard library.
"""

import argparse
import html
import os
import sqlite3
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


def _fetch_one(sql: str) -> tuple | None:
    """Return the first row of ``sql``, or None if there is none or it cannot be read.

    The database is opened read-only, so it is never created or changed.
    """
    try:
        conn = sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return conn.execute(sql).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def count_runs() -> int | None:
    """Return the number of rows in ``runs``, or None if it cannot be read.

    The database is opened read-only, so it is never created or changed.
    """
    row = _fetch_one("SELECT COUNT(*) FROM runs")
    if row is None:
        return None
    return int(row[0])


def latest_run() -> LatestRun | None:
    """Return the run with the highest id, or None if there is no readable run.

    The database is opened read-only, so it is never created or changed.
    """
    row = _fetch_one("SELECT outcome, reason FROM runs ORDER BY id DESC LIMIT 1")
    if row is None:
        return None
    return LatestRun(row[0] or "", row[1] or "")


def _render_latest(latest: LatestRun | None) -> str:
    """Return the page lines for the latest run, each ending in a newline."""
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


def render_page(count: int | None, latest: LatestRun | None = None) -> str:
    """Return the full HTML page for the given run count and latest run."""
    if count is None:
        status = (
            '<p id="run-count-unavailable">Run count unavailable: '
            f"no readable runs table in {html.escape(str(db_path()))}</p>"
        )
    else:
        status = f'<p>Runs recorded: <strong id="run-count">{count}</strong></p>'
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>tomfactory viewer</title>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Hello from the factory</h1>\n"
        f"{status}\n"
        f"{_render_latest(latest)}"
        "</body>\n"
        "</html>\n"
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/":
            self.send_error(404)
            return
        body = render_page(count_runs(), latest_run()).encode("utf-8")
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
