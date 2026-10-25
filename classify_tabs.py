#!/usr/bin/env python3
"""Classify classical guitar text tabs with OpenRouter and save to SQLite (stdlib only)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib import error, request

ROOT = Path(__file__).resolve().parent
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def field(kind, description, nullable=False, **extra):
    return {"type": [kind, "null"] if nullable else kind,
            "description": description, **extra}


PROPERTIES = {
    "title": field("string", "Musical title, including movement when present", True),
    "author": field("string", "Composer's name, not transcriber", True),
    "year": field("integer", "Explicit composition year, otherwise null", True),
    "arranger": field("string", "Arranger or transcriber, if credited", True),
    "catalogue": field("string", "Opus or catalogue number", True),
    "key_signature": field("string", "Key of the arrangement", True),
    "time_signature": field("string", "Meter(s) of the arrangement", True),
    "tuning": field("string", "Guitar tuning", True),
    "estimated_measures": field("integer", "Unique measures in one rendition", True, minimum=0),
    "difficulty": field("string", "Estimated actual playing level",
                        enum=["elementary", "beginner_intermediate", "intermediate",
                              "advanced", "artist", "unknown"]),
    **{name: field("boolean", description) for name, description in {
        "level_ok": "Difficulty fits beginner-intermediate to intermediate",
        "quality_ok": "A complete rendition has readable notes and rhythm",
        "length_ok": "Sufficient musical length and substance",
        "is_real_piece": "Standalone musical substance, including expressive studies/etudes; excludes purely mechanical drills",
        "is_complete": "Complete piece or movement rather than excerpt",
        "is_solo_guitar": "Complete standalone arrangement for one guitarist without required accompaniment",
        "accepted": "True exactly when all six suitability checks pass",
    }.items()},
    "rejection_reason": field("string", "Specific rejection reason; null if accepted", True),
    "assessment": field("string", "Concise musical evidence and uncertainties"),
    "confidence": field("number", "Classification confidence", minimum=0, maximum=1),
}
SCHEMA = {"type": "object", "properties": PROPERTIES,
          "required": list(PROPERTIES), "additionalProperties": False}
CHECKS = ("level_ok", "quality_ok", "length_ok", "is_real_piece", "is_complete", "is_solo_guitar")


def read_tab(path):
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def reference_messages(examples_dir):
    """Include complete references: spacing and alternate versions matter."""
    messages = []
    for label, candidates in (
        ("ACCEPT", ("accepted-tabs", "accepted-examples")),
        ("REJECT", ("rejected-tabs", "rejected-examples")),
    ):
        directory = next((examples_dir / name for name in candidates
                          if (examples_dir / name).is_dir()), None)
        if directory:
            for path in sorted(directory.glob("*.txt")):
                messages.append({"role": "user", "content": json.dumps({
                    "kind": "labeled_reference", "label": label,
                    "filename": path.name, "text": read_tab(path)}, ensure_ascii=False)})
    return messages


def validate_result(result):
    if not isinstance(result, dict) or set(result) != set(PROPERTIES):
        raise ValueError("Response fields do not match the classification schema")
    types = {"string": str, "integer": int, "boolean": bool, "number": (int, float)}
    for name, spec in PROPERTIES.items():
        value = result[name]
        kinds = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        if value is None and "null" in kinds:
            continue
        kind = kinds[0]
        if not isinstance(value, types[kind]) or (kind in ("integer", "number") and isinstance(value, bool)):
            raise ValueError(f"Invalid type for {name}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"Invalid value for {name}")
        if "minimum" in spec and not value >= spec["minimum"]:
            raise ValueError(f"Value below minimum for {name}")
        if "maximum" in spec and not value <= spec["maximum"]:
            raise ValueError(f"Value above maximum for {name}")
    if result["accepted"] != all(result[name] for name in CHECKS):
        raise ValueError("Acceptance contradicts suitability checks")
    reason = result["rejection_reason"]
    if result["accepted"] and reason is not None:
        raise ValueError("Accepted result must have a null rejection reason")
    if not result["accepted"] and (not reason or not reason.strip()):
        raise ValueError("Rejected result must explain why")
    if result["level_ok"] != (result["difficulty"] in ("beginner_intermediate", "intermediate")):
        raise ValueError("Difficulty contradicts level_ok")
    return result


def classify(payload, api_key, timeout=120, retries=2):
    encoded = json.dumps(payload).encode("utf-8")
    for attempt in range(retries + 1):
        req = request.Request(ENDPOINT, data=encoded, headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=timeout) as response:
                envelope = json.load(response)
            choice = envelope["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError(f"Incomplete/refused response: {choice.get('finish_reason')}")
            result = validate_result(json.loads(choice["message"]["content"]))
            return result, envelope.get("usage", {}), envelope.get("model", payload["model"])
        except error.HTTPError as exc:
            # Do not print response bodies: authentication errors may contain secrets.
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == retries:
                raise RuntimeError(f"OpenRouter HTTP {exc.code}; check credentials, model support and quota") from None
            retry_after = exc.headers.get("Retry-After", "")
            delay = min(float(retry_after), 60) if retry_after.isdigit() else 2 ** attempt
        except (error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise RuntimeError(f"OpenRouter connection failed ({type(exc).__name__})") from None
            delay = 2 ** attempt
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid OpenRouter response: {exc}") from None
        time.sleep(delay)


def open_db(path):
    conn = sqlite3.connect(path)
    columns = []
    for name, spec in PROPERTIES.items():
        kind = spec["type"] if isinstance(spec["type"], str) else spec["type"][0]
        sql_type = {"string": "TEXT", "integer": "INTEGER", "boolean": "INTEGER", "number": "REAL"}[kind]
        columns.append(f'{name} {sql_type}')
    conn.execute(f"""CREATE TABLE IF NOT EXISTS classifications (
        filename TEXT PRIMARY KEY, source_path TEXT NOT NULL,
        content_sha256 TEXT NOT NULL, request_sha256 TEXT NOT NULL,
        model TEXT NOT NULL, returned_model TEXT NOT NULL, classified_at TEXT NOT NULL,
        {', '.join(columns)}, result_json TEXT NOT NULL, usage_json TEXT NOT NULL,
        processing_seconds REAL
    )""")
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(classifications)")}
    if "is_solo_guitar" not in existing_columns:
        # Unknown for old results until reclassification; never assume solo guitar.
        conn.execute("ALTER TABLE classifications ADD COLUMN is_solo_guitar INTEGER")
    if "processing_seconds" not in existing_columns:
        conn.execute("ALTER TABLE classifications ADD COLUMN processing_seconds REAL")
    conn.commit()
    return conn


def save_result(conn, path, text, fingerprint, model, returned_model, result, usage,
                processing_seconds=None):
    row = dict(filename=path.name, source_path=str(path.resolve()),
               content_sha256=hashlib.sha256(text.encode()).hexdigest(),
               request_sha256=fingerprint, model=model, returned_model=returned_model,
               classified_at=datetime.now(timezone.utc).isoformat(), **result,
               result_json=json.dumps(result, ensure_ascii=False), usage_json=json.dumps(usage),
               processing_seconds=processing_seconds)
    names = list(row)
    updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != "filename")
    with conn:
        conn.execute(f"INSERT INTO classifications ({', '.join(names)}) "
                     f"VALUES ({', '.join('?' for _ in names)}) "
                     f"ON CONFLICT(filename) DO UPDATE SET {updates}", list(row.values()))


def token_counts(usage):
    usage = usage if isinstance(usage, dict) else {}
    counts = [usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
    counts = [value if type(value) is int and value >= 0 else None for value in counts]
    if counts[2] is None and counts[0] is not None and counts[1] is not None:
        counts[2] = counts[0] + counts[1]
    return counts


def format_tokens(counts):
    return ", ".join(f"{label}={value:,}" if value is not None else f"{label}=unavailable"
                     for label, value in zip(("input", "output", "total"), counts))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="TXT files or directories (default: tabs)")
    parser.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL"), help="OpenRouter model ID; or OPENROUTER_MODEL")
    parser.add_argument("--db", type=Path, default=ROOT / "classifications.sqlite3")
    parser.add_argument("--prompt", type=Path, default=ROOT / "classification_prompt.txt")
    parser.add_argument("--examples-dir", type=Path, default=ROOT)
    examples = parser.add_mutually_exclusive_group()
    examples.add_argument("--examples", action="store_true", help="Include full labeled example texts (default: omitted)")
    examples.add_argument("--no-examples", dest="examples", action="store_false", help="Omit full example texts (default)")
    parser.set_defaults(examples=False)
    parser.add_argument("--force", action="store_true", help="Reclassify even if request is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="Print full first request without API calls or DB writes")
    parser.add_argument("--limit", type=int, help="Maximum number of input files")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1 or args.timeout <= 0 or args.retries < 0:
        parser.error("limit/timeout must be positive and retries nonnegative")
    files = {}
    for path in args.paths or [ROOT / "tabs"]:
        if not path.exists():
            parser.error(f"Input does not exist: {path}")
        for item in sorted(path.rglob("*.txt")) if path.is_dir() else [path]:
            if item.suffix.lower() != ".txt":
                parser.error(f"Expected a TXT file: {item}")
            if item.name in files and files[item.name].resolve() != item.resolve():
                parser.error(f"Duplicate filename (SQLite key): {item.name}")
            files[item.name] = item
    selected = list(files.values())[:args.limit]
    if not selected:
        parser.error("No TXT files found")
    if not args.model:
        parser.error("Set --model or OPENROUTER_MODEL to a model supporting structured outputs")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not args.dry_run and not api_key:
        parser.error("Set OPENROUTER_API_KEY")
    messages = [{"role": "system", "content": args.prompt.read_text(encoding="utf-8")}]
    if args.examples:
        messages += reference_messages(args.examples_dir)
    base = {"model": args.model, "provider": {"require_parameters": True},
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "guitar_tab_classification", "strict": True, "schema": SCHEMA}}}
    conn = None if args.dry_run else open_db(args.db)
    failed = 0
    totals = [0, 0, 0]
    missing_usage = 0
    accepted = rejected = skipped = completed = 0
    batch_started = None
    batch_elapsed = 0.0
    try:
        for path in selected:
            started = time.perf_counter()
            if batch_started is None:
                batch_started = started
            if not args.dry_run:
                print(f"PROCESS {completed + 1}/{len(selected)}: {path.name}", flush=True)
            processing_seconds = None
            try:
                text = read_tab(path)
                payload = dict(base, messages=messages + [{"role": "user", "content": json.dumps({
                    "kind": "classify_this_tab", "filename": path.name, "text": text}, ensure_ascii=False)}])
                if args.dry_run:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                    break
                fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                existing = conn.execute("SELECT request_sha256 FROM classifications WHERE filename=?", (path.name,)).fetchone()
                if not args.force and existing and existing[0] == fingerprint:
                    skipped += 1
                    print(f"SKIP   {path.name} (unchanged)")
                    continue
                result, usage, returned_model = classify(payload, api_key, args.timeout, args.retries)
                processing_seconds = time.perf_counter() - started
                counts = token_counts(usage)
                for index, count in enumerate(counts):
                    totals[index] += count if count is not None else 0
                missing_usage += any(count is None for count in counts)
                print(f"TOKENS {path.name}: {format_tokens(counts)}")
                save_result(conn, path, text, fingerprint, args.model, returned_model, result, usage,
                            processing_seconds)
                accepted += bool(result['accepted'])
                rejected += not result['accepted']
                print(f"{'ACCEPT' if result['accepted'] else 'REJECT'} {path.name}: "
                      f"{result['title'] or '(unknown title)'}"
                      + (f" — {result['rejection_reason']}" if result['rejection_reason'] else ""))
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                failed += 1
                print(f"ERROR  {path.name}: {exc}", file=sys.stderr)
            finally:
                if not args.dry_run:
                    finished = time.perf_counter()
                    elapsed = processing_seconds if processing_seconds is not None else finished - started
                    print(f"TIME   {path.name}: {elapsed:.2f}s")
                    completed += 1
                    batch_elapsed = finished - batch_started
                    eta = batch_elapsed / completed * (len(selected) - completed)
                    print(f"PROGRESS {completed}/{len(selected)} ({completed / len(selected):.0%}): "
                          f"accepted={accepted}, rejected={rejected}, skipped={skipped}, failed={failed}; "
                          f"elapsed={batch_elapsed:.2f}s, ETA={eta:.2f}s", flush=True)
    finally:
        if conn:
            conn.close()
            print(f"RUN SUMMARY: {completed}/{len(selected)} completed; "
                  f"accepted={accepted}, rejected={rejected}, skipped={skipped}, failed={failed}; "
                  f"elapsed={batch_elapsed:.2f}s")
            print(f"RUN TOKENS (reported): {format_tokens(totals)}"
                  + (f"; incomplete usage for {missing_usage} response(s)" if missing_usage else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
