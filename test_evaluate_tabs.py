import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import classify_tabs as classifier
import evaluate_tabs as app
from test_classify_tabs import accepted_result


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.tab = self.root / "piece.txt"
        self.tab.write_text("tab", encoding="utf-8")
        self.digest = hashlib.sha256(b"tab").hexdigest()
        result = accepted_result()
        self.case = {"filename": self.tab.name, "content_sha256": self.digest,
                     "group": "natural", "expected": {field: result[field] for field in app.FIELDS},
                     "confirmed_fields": ["is_complete"]}
        self.document = {"version": 1, "cases": [self.case]}
        self.row = {"filename": self.tab.name, "content_sha256": self.digest,
                    "result_json": json.dumps(result)}

    def evaluate(self, document=None, row=None):
        return app.evaluate(document or self.document,
                            {self.tab.name: row or self.row}, self.root)

    def test_per_rule_disagreement_and_false_reject(self):
        actual = accepted_result() | {"accepted": False, "is_complete": False,
                                      "rejection_reason": "Incomplete excerpt."}
        row = self.row | {"result_json": json.dumps(actual)}
        report = self.evaluate(row=row)
        self.assertEqual(report["summary"]["false_rejects"], 1)
        self.assertEqual(report["summary"]["confirmed"], {"matched": 0, "scored": 1})
        self.assertEqual(set(report["cases"][0]["differences"]), {"is_complete", "accepted"})
        self.assertEqual(report["summary"]["fields"]["quality_ok"]["matched"], 1)

    def test_newline_conventions_match_frozen_labels_but_content_changes_do_not(self):
        for frozen, current in (("tab\nline\n", "tab\r\nline\r\n"),
                                ("tab\r\nline\r\n", "tab\nline\n")):
            with self.subTest(frozen=repr(frozen)):
                self.case["content_sha256"] = hashlib.sha256(frozen.encode()).hexdigest()
                self.tab.write_bytes(current.encode())
                row = self.row | {"content_sha256": hashlib.sha256(current.encode()).hexdigest()}
                self.assertEqual(self.evaluate(row=row)["cases"][0]["status"], "match")
                self.assertEqual(self.evaluate()["cases"][0]["status"], "stale_prediction")
                self.tab.write_bytes(current.replace("line", "changed").encode())
                self.assertEqual(self.evaluate(row=row)["cases"][0]["status"], "changed_source")

    def test_false_accept_and_synthetic_group(self):
        self.case["expected"].update(is_complete=False, accepted=False)
        self.case["group"] = "synthetic"
        report = self.evaluate()
        self.assertEqual(report["summary"]["false_accepts"], 1)
        self.assertEqual(report["groups"]["synthetic"]["cases"], 1)
        self.assertEqual(report["groups"]["natural"]["cases"], 0)

    def test_unscored_disputed_label_does_not_count_as_mismatch(self):
        self.case["expected"].update(is_real_piece=None, accepted=None)
        actual = accepted_result() | {"accepted": False, "is_real_piece": False,
                                      "rejection_reason": "Mechanical drill."}
        report = self.evaluate(row=self.row | {"result_json": json.dumps(actual)})
        self.assertEqual(report["summary"]["exact_matches"], 1)
        self.assertEqual(report["summary"]["fields"]["is_real_piece"]["scored"], 0)

    def test_missing_stale_changed_and_invalid_predictions_are_not_matches(self):
        report = app.evaluate(self.document, {}, self.root)
        self.assertEqual(report["cases"][0]["status"], "missing_prediction")
        report = self.evaluate(row=self.row | {"content_sha256": "0" * 64})
        self.assertEqual(report["cases"][0]["status"], "stale_prediction")
        report = self.evaluate(row=self.row | {"result_json": "{}"})
        self.assertEqual(report["cases"][0]["status"], "invalid_prediction")
        self.tab.write_text("changed")
        report = self.evaluate()
        self.assertEqual(report["cases"][0]["status"], "changed_source")
        self.assertEqual(report["summary"]["evaluated"], 0)
        self.assertEqual(report["summary"]["unavailable"], 1)
        self.tab.unlink()
        self.assertEqual(self.evaluate()["cases"][0]["status"], "missing_source")

    def test_duplicate_and_contradictory_labels_rejected(self):
        labels = self.root / "labels.json"
        for change in ("duplicate", "acceptance", "boolean"):
            document = copy.deepcopy(self.document)
            if change == "duplicate":
                document["cases"].append(copy.deepcopy(document["cases"][0]))
            elif change == "acceptance":
                document["cases"][0]["expected"]["is_complete"] = False
            else:
                document["cases"][0]["expected"]["quality_ok"] = 1
            labels.write_text(json.dumps(document))
            with self.subTest(change=change), self.assertRaises(ValueError):
                app.load_labels(labels)

    def test_fresh_run_failure_cannot_reuse_previous_success(self):
        db = self.root / "existing.sqlite3"
        with contextlib.closing(classifier.open_db(db)) as conn:
            classifier.save_result(conn, self.tab, "tab", "request", "test", "test",
                                   accepted_result(), {})
        original = db.read_bytes()
        labels = self.root / "labels.json"
        labels.write_text(json.dumps(self.document))
        report = self.root / "report.json"
        args = ["--labels", str(labels), "--tabs-dir", str(self.root), "--db", str(db),
                "--report", str(report)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(args), 0)
        with patch.dict(classifier.os.environ, {"OPENROUTER_API_KEY": "test"}), \
                patch.object(classifier, "classify", side_effect=RuntimeError("Offline")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(args + ["--run", "--model", "test"]), 1)
        self.assertEqual(json.loads(report.read_text())["cases"][0]["status"], "missing_prediction")
        self.assertEqual(db.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
