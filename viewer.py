"""Hello-world observability page for the factory.

Serves one page that says hello and shows how many runs the factory has
recorded. Uses only the Python standard library.
"""

import argparse
import html
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def db_path() -> Path:
    """Return the path of the factory database, read from the environment now."""
    home = os.environ.get("TOMFACTORY_HOME")
    if not home:
        return Path("~/.tomfactory").expanduser() / "factory.db"
    return Path(home) / "factory.db"


def count_runs() -> int | None:
    """Return the number of rows in ``runs``, or None if it cannot be read.

    The database is opened read-only, so it is never created or changed.
    """
    try:
        conn = sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0])


def render_page(count: int | None) -> str:
    """Return the full HTML page for the given run count."""
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
        "</body>\n"
        "</html>\n"
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/":
            self.send_error(404)
            return
        body = render_page(count_runs()).encode("utf-8")
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
