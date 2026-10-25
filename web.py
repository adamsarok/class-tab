#!/usr/bin/env python3
"""Browse local tab classifications: python web.py [--db PATH] [--port 8000]."""

import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import unquote, urlsplit

from classify_tabs import ROOT, read_tab, token_counts


def connect(db):
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def handler_for(db):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, content, content_type):
            body = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    self.respond(200, (ROOT / "web/index.html").read_text(encoding="utf-8"), "text/html")
                elif path == "/api/tabs":
                    with contextlib.closing(connect(db)) as conn:
                        rows = [dict(row) for row in conn.execute("SELECT * FROM classifications ORDER BY filename")]
                    for row in rows:
                        try:
                            usage = json.loads(row.get("usage_json") or "{}")
                        except (ValueError, TypeError):
                            usage = {}
                        row.update(zip(("input_tokens", "output_tokens", "total_tokens"), token_counts(usage)))
                    self.respond(200, json.dumps(rows, ensure_ascii=False), "application/json")
                elif path.startswith("/tabs/"):
                    filename = unquote(path[len("/tabs/"):])
                    with contextlib.closing(connect(db)) as conn:
                        row = conn.execute("SELECT source_path FROM classifications WHERE filename=?",
                                           (filename,)).fetchone()
                    if row is None:
                        self.respond(404, "Tab not found.", "text/plain")
                        return
                    self.respond(200, read_tab(Path(row["source_path"])), "text/plain")
                else:
                    self.respond(404, "Not found.", "text/plain")
            except (FileNotFoundError, NotADirectoryError):
                self.respond(404, "The source file is no longer available.", "text/plain")
            except (sqlite3.Error, OSError, UnicodeError):
                self.respond(503, "Could not read the database or tab. Check the server's database path and file access.", "text/plain")

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "classifications.sqlite3")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("Database does not exist; run classify_tabs.py first or pass --db.")
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.db)) as server:
        print(f"Tab library: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
