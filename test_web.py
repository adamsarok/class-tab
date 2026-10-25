import contextlib
from http.server import ThreadingHTTPServer
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

import classify_tabs as app
from test_classify_tabs import accepted_result
from web import handler_for


class WebTests(unittest.TestCase):
    def test_acceptance_filter_options(self):
        class OptionsParser(HTMLParser):
            in_acceptance = False

            def __init__(self):
                super().__init__()
                self.options = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "select":
                    self.in_acceptance = attrs.get("id") == "acceptance"
                elif tag == "option" and self.in_acceptance:
                    self.options.append(attrs.get("value"))

            def handle_endtag(self, tag):
                if tag == "select":
                    self.in_acceptance = False

        parser = OptionsParser()
        parser.feed((app.ROOT / "web/index.html").read_text(encoding="utf-8"))
        self.assertEqual(parser.options, ["", "1", "0", "unknown"])

    def test_library_and_source_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "results.sqlite3"
            tab = root / "piece #1.txt"
            tab.write_bytes(b"Title\r\ne|--0---|\r\nLegacy: \xe9")
            with contextlib.closing(app.open_db(db)) as conn:
                app.save_result(conn, tab, app.read_tab(tab), "hash", "test", "test",
                                accepted_result(), {}, 2.5)
            handler = handler_for(db)
            handler.log_message = lambda *args: None
            with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    with urlopen(base + "/") as response:
                        html = response.read().decode()
                        self.assertIn("Tab library", html)
                        self.assertIn("a.target='_blank'", html)
                    with urlopen(base + "/api/tabs") as response:
                        rows = json.load(response)
                        self.assertEqual(rows[0]["filename"], tab.name)
                        self.assertEqual(rows[0]["processing_seconds"], 2.5)
                    with urlopen(base + "/tabs/piece%20%231.txt") as response:
                        self.assertEqual(response.headers.get_content_type(), "text/plain")
                        self.assertEqual(response.read().decode(), app.read_tab(tab))
                    for path in ("/tabs/../../classification_prompt.txt", "/classification_prompt.txt"):
                        with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                            urlopen(base + path)
                        self.assertEqual(error.exception.code, 404)
                    tab.unlink()
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base + "/tabs/piece%20%231.txt")
                    self.assertEqual(error.exception.code, 404)
                finally:
                    server.shutdown()
                    worker.join()


if __name__ == "__main__":
    unittest.main()
