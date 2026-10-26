#!/usr/bin/env python3
"""Browse local tab classifications: python web.py [--db PATH] [--port 8000]."""

import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import unquote, urlsplit

from classify_tabs import ROOT, PROPERTIES, read_tab


def connect(db):
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def handler_for(db):
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS user_ratings (filename TEXT PRIMARY KEY, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5))")

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
                        fields = ["filename"] + [key for key in PROPERTIES if key != "confidence"]
                        selection = ", ".join("c." + key for key in fields)
                        rows = [dict(row) for row in conn.execute(
                            f"SELECT {selection}, r.rating FROM classifications c LEFT JOIN user_ratings r ON c.filename=r.filename ORDER BY c.filename")]
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

        def do_POST(self):
            if urlsplit(self.path).path != "/api/rating":
                self.respond(404, "Not found.", "text/plain")
                return
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.respond(403, "Origin not allowed.", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096 or self.headers.get_content_type() != "application/json":
                    raise ValueError()
                body = json.loads(self.rfile.read(length))
                filename, rating = body["filename"], body["rating"]
                if not isinstance(filename, str) or (rating is not None and (type(rating) is not int or not 1 <= rating <= 5)):
                    raise ValueError()
                with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                    if not conn.execute("SELECT 1 FROM classifications WHERE filename=?", (filename,)).fetchone():
                        self.respond(404, "Tab not found.", "text/plain")
                        return
                    if rating is None:
                        conn.execute("DELETE FROM user_ratings WHERE filename=?", (filename,))
                    else:
                        conn.execute("INSERT INTO user_ratings VALUES (?, ?) ON CONFLICT(filename) DO UPDATE SET rating=excluded.rating", (filename, rating))
                self.respond(200, json.dumps({"rating": rating}), "application/json")
            except (ValueError, KeyError, TypeError, UnicodeError):
                self.respond(400, "Provide a filename and a rating from 1 to 5, or null to clear.", "text/plain")
            except (sqlite3.Error, OSError):
                self.respond(503, "Could not save rating.", "text/plain")

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
