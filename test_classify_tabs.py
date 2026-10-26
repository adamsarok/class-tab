import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import classify_tabs as app


def accepted_result():
    result = {key: None for key in app.PROPERTIES}
    result.update(title="Lagrima", author="Francisco Tarrega", difficulty="intermediate",
                  estimated_measures=16, assessment="Complete miniature; readable meter and spacing.",
                  confidence=0.9, accepted=True, **{key: True for key in app.CHECKS})
    return result


class ClassifierTests(unittest.TestCase):
    def test_response_requires_explanations(self):
        for name in ("assessment", "rejection_reason"):
            self.assertIn(name, app.SCHEMA["required"])
            result = accepted_result()
            del result[name]
            with self.assertRaises(ValueError):
                app.validate_result(result)
        for changes in ({"assessment": None},
                        {"accepted": False, "quality_ok": False}):
            with self.assertRaises(ValueError):
                app.validate_result(accepted_result() | changes)

    def test_strata_configuration_and_endpoint_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tab = root / "piece.txt"
            tab.write_text("tab")
            args = [str(tab), "--db", str(root / "results.sqlite3"), "--provider", "strata"]
            with patch.dict(app.os.environ, {"STRATA_MODEL": "local-model",
                                            "OPENROUTER_API_KEY": "cloud-secret"}, clear=True), \
                    patch.object(app, "classify", return_value=(accepted_result(), {}, "local-model")) as call, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(args), 0)
                self.assertEqual(call.call_args.kwargs["endpoint"],
                                 "http://127.0.0.1:8080/v1/chat/completions")
                self.assertIsNone(call.call_args.args[1])
                payload = call.call_args.args[0]
                self.assertNotIn("provider", payload)
                self.assertEqual(payload["response_format"]["json_schema"]["schema"], app.SCHEMA)
                self.assertEqual(app.main(args), 0)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(app.main(args + ["--base-url", "http://localhost:8399/v1/"]), 0)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(call.call_args.kwargs["endpoint"],
                                 "http://localhost:8399/v1/chat/completions")

    def test_strata_transport_optional_auth_and_validation(self):
        endpoint = "http://localhost:8080/v1/chat/completions"
        for key in (None, "local-secret"):
            response = io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(accepted_result())}}]}).encode())
            with patch.object(app.request, "urlopen", return_value=response) as call:
                result, _, model = app.classify({"model": "local"}, key, endpoint=endpoint)
            req = call.call_args.args[0]
            self.assertEqual(req.full_url, endpoint)
            self.assertEqual(req.get_header("Authorization"), f"Bearer {key}" if key else None)
            self.assertTrue(result["accepted"])
            self.assertEqual(model, "local")
        response = io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": "{}"}}]}).encode())
        with patch.object(app.request, "urlopen", return_value=response), self.assertRaises(ValueError):
            app.classify({"model": "local"}, None, endpoint=endpoint)

    def test_batch_progress_mixed_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a", "b", "c", "d"):
                (root / f"{name}.txt").write_text("tab")
            args = [str(root), "--db", str(root / "results.sqlite3"), "--model", "test"]
            rejected = accepted_result() | {"accepted": False, "quality_ok": False,
                                            "rejection_reason": "Unreadable rhythm."}
            output = io.StringIO()
            with patch.dict(app.os.environ, {"OPENROUTER_API_KEY": "test"}), \
                    patch.object(app, "classify", return_value=(accepted_result(), {}, "test")) as classify, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(args + ["--limit", "1"]), 0)
                classify.side_effect = [(accepted_result(), {}, "test"),
                                        (rejected, {}, "test"), RuntimeError("Unavailable")]
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
                        patch.object(app.time, "perf_counter",
                                     side_effect=[0, 1, 1, 3, 4, 4, 7, 8, 8, 12]):
                    self.assertEqual(app.main(args), 1)
            self.assertIn("PROCESS 1/4: a.txt", output.getvalue())
            self.assertIn("PROGRESS 1/4 (25%): accepted=0, rejected=0, skipped=1, failed=0; "
                          "elapsed=1.00s, ETA=3.00s", output.getvalue())
            self.assertIn("PROGRESS 4/4 (100%): accepted=1, rejected=1, skipped=1, failed=1; "
                          "elapsed=12.00s, ETA=0.00s", output.getvalue())
            self.assertIn("RUN SUMMARY: 4/4 completed; accepted=1, rejected=1, skipped=1, failed=1",
                          output.getvalue())

    def test_dry_run_output_remains_json(self):
        with tempfile.TemporaryDirectory() as directory:
            tab = Path(directory) / "piece.txt"
            tab.write_text("tab")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(app.main([str(tab), "--model", "test", "--dry-run"]), 0)
            self.assertEqual(json.loads(output.getvalue())["model"], "test")

    def test_processing_time_success_skip_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tab = root / "piece.txt"
            tab.write_text("tab")
            db = root / "results.sqlite3"
            args = [str(tab), "--db", str(db), "--model", "test"]
            output = io.StringIO()
            with patch.dict(app.os.environ, {"OPENROUTER_API_KEY": "test"}), \
                    patch.object(app, "classify", return_value=(accepted_result(), {}, "test")) as classify, \
                    patch.object(app.time, "perf_counter", side_effect=[10, 12.5, 12.6, 20, 20.01, 30, 34]), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(args), 0)
                self.assertEqual(app.main(args), 0)
                classify.side_effect = RuntimeError("API unavailable")
                self.assertEqual(app.main(args + ["--force"]), 1)
            for duration in ("2.50s", "0.01s", "4.00s"):
                self.assertIn(f"TIME   piece.txt: {duration}", output.getvalue())
            with contextlib.closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT processing_seconds FROM classifications").fetchone(),
                                 (2.5,))

    def test_token_counts(self):
        self.assertEqual(app.token_counts({"prompt_tokens": 120, "completion_tokens": 30}),
                         [120, 30, 150])
        self.assertEqual(app.token_counts(None), [None, None, None])
        self.assertEqual(app.token_counts({"prompt_tokens": True, "completion_tokens": -1}),
                         [None, None, None])
        self.assertEqual(app.format_tokens([1200, 30, 1230]),
                         "input=1,200, output=30, total=1,230")

    def test_console_token_totals_exclude_skipped_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "piece.txt").write_text("tab")
            args = [str(root), "--db", str(root / "results.sqlite3"),
                    "--model", "test", "--no-examples"]
            output = io.StringIO()
            with patch.dict(app.os.environ, {"OPENROUTER_API_KEY": "test"}), \
                    patch.object(app, "classify", return_value=(accepted_result(), {
                        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}, "test")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(app.main(args), 0)
                self.assertEqual(app.main(args), 0)
            self.assertIn("TOKENS piece.txt: input=100, output=20, total=120", output.getvalue())
            self.assertIn("RUN TOKENS (reported): input=100, output=20, total=120", output.getvalue())
            self.assertIn("RUN TOKENS (reported): input=0, output=0, total=0", output.getvalue())

    def test_non_solo_arrangement_requires_rejection(self):
        duet = accepted_result() | {"is_solo_guitar": False}
        with self.assertRaises(ValueError):
            app.validate_result(duet)
        duet.update(accepted=False, rejection_reason="Guitar duet requires two performers.")
        self.assertEqual(app.validate_result(duet), duet)

    def test_existing_database_migration_preserves_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "legacy.sqlite3"
            with contextlib.closing(app.open_db(db)) as conn:
                app.save_result(conn, Path("solo.txt"), "tab", "old-request", "test", "test",
                                accepted_result(), {})
                conn.execute("ALTER TABLE classifications DROP COLUMN is_solo_guitar")
                conn.execute("ALTER TABLE classifications DROP COLUMN processing_seconds")
                conn.commit()
            with contextlib.closing(app.open_db(db)) as conn:
                self.assertEqual(conn.execute("SELECT processing_seconds FROM classifications").fetchone(),
                                 (None,))
                self.assertEqual(conn.execute(
                    "SELECT filename, title, is_solo_guitar FROM classifications").fetchall(),
                    [("solo.txt", "Lagrima", None)])
                duet = accepted_result() | {"is_solo_guitar": False, "accepted": False,
                                           "rejection_reason": "Requires two guitarists."}
                app.save_result(conn, Path("solo.txt"), "tab", "new-request", "test", "test", duet, {})
            with contextlib.closing(app.open_db(db)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT is_solo_guitar, accepted, rejection_reason FROM classifications").fetchone(),
                    (0, 0, "Requires two guitarists."))

    def test_invalid_results_are_rejected(self):
        for changes in ({"accepted": False}, {"confidence": 2},
                        {"level_ok": "true"}, {"difficulty": "artist"},
                        {"estimated_measures": True}, {"rejection_reason": "No"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                app.validate_result(accepted_result() | changes)
        rejected = accepted_result() | {"accepted": False, "quality_ok": False,
                                       "rejection_reason": "Rhythm cannot be read."}
        self.assertEqual(app.validate_result(rejected), rejected)

    def test_api_schema_and_response(self):
        response = io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(accepted_result())}}], "model": "returned-model"}).encode())
        with patch.object(app.request, "urlopen", return_value=response) as call:
            result, _, model = app.classify({"model": "test"}, "secret")
        self.assertTrue(result["accepted"])
        self.assertEqual(model, "returned-model")
        self.assertEqual(call.call_args.args[0].get_header("Authorization"), "Bearer secret")

    def test_resume_update_and_failure_preserve_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tab = root / "piece.txt"
            tab.write_text("first", encoding="utf-8")
            db = root / "results.sqlite3"
            args = [str(tab), "--db", str(db), "--model", "test-model", "--no-examples"]
            with patch.dict(app.os.environ, {"OPENROUTER_API_KEY": "test"}), \
                    patch.object(app, "classify", return_value=(accepted_result(), {}, "test-model")) as classify, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(args), 0)
                self.assertEqual(app.main(args), 0)
                self.assertEqual(classify.call_count, 1)
                tab.write_text("changed", encoding="utf-8")
                self.assertEqual(app.main(args), 0)
                self.assertEqual(classify.call_count, 2)
                classify.side_effect = RuntimeError("API unavailable")
                self.assertEqual(app.main(args + ["--force"]), 1)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT filename, title, accepted FROM classifications").fetchall(),
                                 [("piece.txt", "Lagrima", 1)])

    def test_duplicate_filenames_fail_before_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "same.txt").write_text("tab")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.main([str(root), "--model", "test", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
