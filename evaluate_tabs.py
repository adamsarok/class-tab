#!/usr/bin/env python3
"""Compare stored or freshly generated classifications against frozen tab labels."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

import classify_tabs as classifier

ROOT = classifier.ROOT
FIELDS = (*classifier.CHECKS, "accepted", "difficulty")
GROUPS = ("natural", "calibration", "synthetic")


def matches_frozen_source(text, expected_digest):
    # Older labels preserve the source's original newline convention.
    # Accept either convention without changing the text sent to the classifier.
    lf = text.replace("\r\n", "\n")
    return any(hashlib.sha256(candidate.encode()).hexdigest() == expected_digest
               for candidate in (text, lf, lf.replace("\n", "\r\n")))


def load_labels(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != 1 or not document.get("cases"):
        raise ValueError("Expected a version 1 label file with nonempty cases")
    names = set()
    for case in document["cases"]:
        name = case["filename"]
        if not name or "/" in name or "\\" in name or name in names:
            raise ValueError(f"Invalid or duplicate filename: {name}")
        names.add(name)
        digest = case["content_sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"Invalid content hash: {name}")
        if case["group"] not in GROUPS:
            raise ValueError(f"Invalid group: {name}")
        expected = case["expected"]
        if set(expected) != set(FIELDS):
            raise ValueError(f"Expected fields must be {', '.join(FIELDS)}: {name}")
        for field, value in expected.items():
            if value is None:
                continue
            if field == "difficulty":
                valid = value in classifier.PROPERTIES["difficulty"]["enum"]
            else:
                valid = type(value) is bool
            if not valid:
                raise ValueError(f"Invalid expected {field}: {name}")
        checks = [expected[field] for field in classifier.CHECKS]
        if (expected["accepted"] is True and False in checks or
                all(value is not None for value in checks) and
                expected["accepted"] is not None and expected["accepted"] != all(checks)):
            raise ValueError(f"Acceptance contradicts checks: {name}")
        difficulty = expected["difficulty"]
        if difficulty is not None and expected["level_ok"] is not None:
            if expected["level_ok"] != (difficulty in ("beginner_intermediate", "intermediate")):
                raise ValueError(f"Difficulty contradicts level_ok: {name}")
        confirmed = case.get("confirmed_fields", [])
        if any(field not in FIELDS or expected[field] is None for field in confirmed):
            raise ValueError(f"Invalid confirmed fields: {name}")
    return document


def read_predictions(db):
    with contextlib.closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return {row["filename"]: dict(row) for row in conn.execute("SELECT * FROM classifications")}


def summarize(cases):
    valid = [case for case in cases if case["status"] in ("match", "mismatch")]
    fields = {}
    for field in FIELDS:
        scored = [case for case in valid if case["expected"][field] is not None]
        matches = sum(field not in case["differences"] for case in scored)
        fields[field] = {"matched": matches, "scored": len(scored),
                         "accuracy": matches / len(scored) if scored else None}
    confirmed = [(case, field) for case in valid for field in case["confirmed_fields"]]
    coverage = {}
    for field in classifier.CHECKS:
        coverage[field] = {"true": sum(case["expected"][field] is True for case in cases),
                           "false": sum(case["expected"][field] is False for case in cases),
                           "unscored": sum(case["expected"][field] is None for case in cases)}
    return {
        "cases": len(cases), "evaluated": len(valid),
        "exact_matches": sum(case["status"] == "match" for case in valid),
        "unavailable": len(cases) - len(valid),
        "false_accepts": sum(case["expected"]["accepted"] is False and
                             case["actual"]["accepted"] is True for case in valid),
        "false_rejects": sum(case["expected"]["accepted"] is True and
                             case["actual"]["accepted"] is False for case in valid),
        "confirmed": {"matched": sum(field not in case["differences"] for case, field in confirmed),
                      "scored": len(confirmed)},
        "fields": fields, "label_coverage": coverage,
    }


def evaluate(document, predictions, tabs_dir):
    cases = []
    for label in document["cases"]:
        case = {"filename": label["filename"], "group": label["group"],
                "expected": label["expected"], "confirmed_fields": label.get("confirmed_fields", []),
                "differences": {}, "actual": None}
        row = predictions.get(label["filename"])
        try:
            text = classifier.read_tab(tabs_dir / label["filename"])
            digest = hashlib.sha256(text.encode()).hexdigest()
        except (OSError, UnicodeError):
            case.update(status="missing_source", error="Source tab is missing or unreadable")
        else:
            if not matches_frozen_source(text, label["content_sha256"]):
                case.update(status="changed_source", error="Source differs from frozen label hash")
            elif row is None:
                case.update(status="missing_prediction", error="No classification available")
            elif row["content_sha256"] != digest:
                case.update(status="stale_prediction", error="Classification is for different source content")
            else:
                try:
                    # Validate the actual structured response rather than treating null flags as false.
                    actual = classifier.validate_result(json.loads(row["result_json"]))
                    case["actual"] = {field: actual[field] for field in FIELDS}
                    case["differences"] = {
                        field: {"expected": value, "actual": actual[field]}
                        for field, value in label["expected"].items()
                        if value is not None and value != actual[field]
                    }
                    case["status"] = "mismatch" if case["differences"] else "match"
                    case["prediction"] = {key: row.get(key) for key in (
                        "model", "returned_model", "classified_at", "request_sha256",
                        "content_sha256", "processing_seconds", "usage_json")}
                    case["prediction"]["assessment"] = actual["assessment"]
                    case["prediction"]["rejection_reason"] = actual["rejection_reason"]
                except (ValueError, TypeError, KeyError) as exc:
                    case.update(status="invalid_prediction", error=str(exc))
        cases.append(case)
    return {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summarize(cases),
        "groups": {group: summarize([case for case in cases if case["group"] == group])
                   for group in GROUPS},
        "cases": cases,
    }


def print_report(report):
    print("Labels include model-generated draft expectations; agreement measures baseline stability.")
    for group, summary in [("ALL", report["summary"]), *report["groups"].items()]:
        print(f"{group}: {summary['exact_matches']}/{summary['evaluated']} exact matches; "
              f"{summary['unavailable']} unavailable; false accepts={summary['false_accepts']}, "
              f"false rejects={summary['false_rejects']}")
    confirmed = report["summary"]["confirmed"]
    print(f"User-confirmed expectations: {confirmed['matched']}/{confirmed['scored']} matched")
    for field, score in report["summary"]["fields"].items():
        coverage = report["summary"]["label_coverage"].get(field)
        detail = (f"; labels: true={coverage['true']}, false={coverage['false']}, "
                  f"unscored={coverage['unscored']}" if coverage else "")
        print(f"  {field}: {score['matched']}/{score['scored']} matched{detail}")
    for case in report["cases"]:
        if case["status"] == "mismatch":
            differences = "; ".join(f"{field}: expected={values['expected']}, actual={values['actual']}"
                                    for field, values in case["differences"].items())
            print(f"DIFF {case['filename']}: {differences}")
        elif case["status"] != "match":
            print(f"UNAVAILABLE {case['filename']}: {case['error']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=ROOT / "evaluation/labels.json")
    parser.add_argument("--db", type=Path, default=ROOT / "classifications.sqlite3",
                        help="Existing predictions to score (default: main database; read-only)")
    parser.add_argument("--tabs-dir", type=Path, default=ROOT / "tabs")
    parser.add_argument("--report", type=Path, default=ROOT / "evaluation/reports/latest.json")
    parser.add_argument("--run", action="store_true", help="Make fresh API calls in an isolated temporary database")
    parser.add_argument("--model", default=None, help="Model for --run (otherwise the selected provider's environment variable)")
    parser.add_argument("--provider", choices=("openrouter", "strata"), default=None)
    parser.add_argument("--base-url", help="Strata API base URL for --run")
    parser.add_argument("--prompt", type=Path, default=ROOT / "classification_prompt.txt")
    parser.add_argument("--examples", action="store_true", help="Include full references during --run")
    parser.add_argument("--examples-dir", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--debug", action="store_true", help="Stream model response chunks during --run")
    parser.add_argument("--reasoning", choices=classifier.REASONING_LEVELS,
                        help="Reasoning effort during --run (default: omit reasoning controls)")
    args = parser.parse_args(argv)
    try:
        document = load_labels(args.labels)
        if args.report.resolve() in (args.labels.resolve(), args.db.resolve()):
            raise ValueError("Report must not overwrite labels or the classification database")
        if args.report.resolve() in {(args.tabs_dir / case["filename"]).resolve()
                                     for case in document["cases"]}:
            raise ValueError("Report must not overwrite an evaluation source tab")
        if args.run:
            # Check every frozen input before making the first paid API call.
            for case in document["cases"]:
                path = args.tabs_dir / case["filename"]
                text = classifier.read_tab(path)
                if not matches_frozen_source(text, case["content_sha256"]):
                    raise ValueError(f"Source differs from frozen labels: {case['filename']}")
            with tempfile.TemporaryDirectory() as directory:
                db = Path(directory) / "predictions.sqlite3"
                command = [str(args.tabs_dir / case["filename"]) for case in document["cases"]]
                command += ["--db", str(db), "--prompt", str(args.prompt),
                            "--timeout", str(args.timeout), "--retries", str(args.retries),
                            "--examples-dir", str(args.examples_dir)]
                if args.model:
                    command += ["--model", args.model]
                if args.provider:
                    command += ["--provider", args.provider]
                if args.base_url:
                    command += ["--base-url", args.base_url]
                if args.examples:
                    command += ["--examples"]
                if args.debug:
                    command += ["--debug"]
                if args.reasoning is not None:
                    command += ["--reasoning", args.reasoning]
                classifier.main(command)
                predictions = read_predictions(db)
        else:
            predictions = read_predictions(args.db)
        report = evaluate(document, predictions, args.tabs_dir)
        report["mode"] = "fresh_run" if args.run else "stored_predictions"
        report["labels_sha256"] = hashlib.sha256(args.labels.read_bytes()).hexdigest()
        report["label_description"] = document.get("description")
        print_report(report)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Report: {args.report}")
        summary = report["summary"]
        return int(summary["unavailable"] > 0 or summary["exact_matches"] < summary["evaluated"])
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
