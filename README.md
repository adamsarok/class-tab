# Classical guitar tab classifier

Python 3.10+; no third-party packages. Classifies complete `.txt` guitar tabs using
OpenRouter, then stores results in SQLite with **filename as the primary key**.

PowerShell:

```powershell
$env:OPENROUTER_API_KEY = 'your-key'
$env:OPENROUTER_MODEL = 'your-provider/model-id'
python classify_tabs.py --limit 1
python classify_tabs.py
```

Choose an OpenRouter model/endpoint supporting structured outputs. The script sends
a strict JSON schema and requires provider support, as described in the
[OpenRouter structured output documentation](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/structured-outputs.mdx).
API calls send the tab (and reference texts when enabled) to OpenRouter and incur the selected
model's usage charges. Credentials are read from environment variables; `.env`
files are not loaded automatically.

Useful commands:

```powershell
# Individual file or directories; directories are searched recursively
python classify_tabs.py tabs/tarrega_lagrima.txt --model your-provider/model-id
python classify_tabs.py tabs --db results.sqlite3

# Inspect the full first request without calling the API or creating a DB
python classify_tabs.py --model your-provider/model-id --dry-run

# Reclassify existing results, or opt into full example texts
python classify_tabs.py --force
python classify_tabs.py --examples

# Offline tests
python -m unittest -v
```

To browse results in a local web frontend:

```powershell
python web.py
# Optional: choose another database or port
python web.py --db results.sqlite3 --port 8080
```

Open `http://127.0.0.1:8000` (or your chosen port). No extra packages or API key
are required. The table has search across displayed columns, acceptance and
difficulty filters, and sortable column headers. Columns include metadata,
suitability checks, assessment, confidence, processing time, token counts, and
model details. Use Refresh to load new classifications without restarting.
Filename links open the original text in a new browser tab, preserving tab
spacing. Source files must still exist at their stored paths; moved or deleted
files return a missing-file message. The server binds to localhost and reads the
database without changing it. Press Ctrl+C to stop.

The editable rubric is in `classification_prompt.txt`. Full example texts are
omitted by default; the rubric's brief calibration descriptions remain included.
Use `--examples` to include complete labeled texts from `accepted-examples` and
`rejected-examples`. `--no-examples` remains supported and explicitly selects the default.
The names `accepted-tabs` and `rejected-tabs` are also supported and take priority
when present. `--examples-dir PATH` changes their parent directory. Full examples
increase token usage; select a context window large enough for examples plus the
entire target. Files are never silently truncated. UTF-8 and legacy Windows-1252
text are supported, and tab alignment is preserved.

Acceptance requires all of: appropriate level, readable notes/rhythm, adequate
length, standalone musical substance, completeness, and an arrangement for a
single guitarist (`is_solo_guitar`). Duets, ensembles and guitar parts requiring
another performer are rejected; complete solo reductions can qualify.
Studies and etudes can
qualify as repertoire; their label or pedagogical origin is never a rejection
reason. Only purely mechanical drills lacking standalone musical substance fail
the `is_real_piece` check. All other suitability checks still apply.
The prompt is calibrated to the supplied examples, including accepting Lagrima
despite its short form and position changes. Missing conventional staff notation
is acceptable if ASCII spacing, meter and duration marks make rhythm readable.
These are subjective LLM estimates; review the assessment and confidence when
deciding on borderline pieces.

The `classifications` table includes title, author (composer), composition year,
arranger, catalogue, key, meter, tuning, estimated unique measures, difficulty,
six suitability booleans, `accepted`, `rejection_reason`, `assessment`, and
`confidence`. Unknown metadata is null; composition year is not inferred from
edition dates or composer life dates. Booleans are stored as SQLite integers.
`result_json` preserves the structured classification; `usage_json` preserves API
usage. Model IDs, UTC timestamp, source path and content/request hashes are stored
for provenance.

Console output shows input, output and total tokens per classification and reported
totals for the current run. Counts come from OpenRouter's response usage; skipped
files add no tokens. Missing counts display as unavailable and mark run totals as
incomplete. Failed calls without a validated response are not included in totals.

Each tab prints `TIME filename: 12.34s`, including failed and skipped inputs.
Batch output identifies the current file before processing, then reports completed/
total files, percentage, accepted/rejected/skipped/failed counts, elapsed seconds,
and an estimated remaining time after each file. ETA uses the average elapsed time
per completed file, including skips and failures; mixed batches can make it vary.
A final `RUN SUMMARY` repeats the outcome counts and elapsed time. `--limit` sets
the batch size when supplied. Progress is omitted for dry runs.
Successful classifications store `processing_seconds` in SQLite, measured from
reading the input through response validation, including API retries and waits,
but excluding the database write. Skips report only the current skip check time
and preserve the stored classification duration. Dry runs omit timing output.
Existing databases automatically gain this column; historical durations are null
until reclassified.

Existing databases automatically gain the `is_solo_guitar` column on the next run.
Old rows have null in this column until reclassified. The changed prompt/schema
automatically triggers reclassification, so rerun the script to apply the rule.

Example query:

```sql
SELECT filename, title, author, difficulty
FROM classifications WHERE accepted = 1 ORDER BY author, title;
```

Successful classifications commit after each file. Identical requests are skipped;
changing the tab, prompt, examples, schema or model triggers reclassification.
`--force` overrides skipping. Duplicate basenames across input directories are
rejected before API calls to avoid key collisions. A changed file updates its row.
Transient connection/rate/server errors retry with bounded waits. Invalid model
output or a failed call produces an error, continues to the next file, returns a
nonzero exit code, and preserves any existing successful row. Stored rows therefore
represent the last successful classification; hashes identify its source version.

Labeled evaluation uses the frozen 27-case set in `evaluation/labels.json`:

```powershell
# Offline comparison of the current database against the frozen labels
python evaluate_tabs.py

# Reclassify only the labeled files with the current prompt and chosen model
# Requires OPENROUTER_API_KEY and incurs normal API charges
python evaluate_tabs.py --run --model your-provider/model-id

# Compare a different existing database, or retain a named report
python evaluate_tabs.py --db other-results.sqlite3 --report evaluation/reports/comparison.json
```

The evaluator reports exact case agreement, per-rule agreement, false accepts and
false rejects, and label coverage. Natural, prompt-calibration, and synthetic
cases are summarized separately. Missing files/results, changed source contents,
invalid responses, and stale classifications are reported as unavailable rather
than matches. Exit status is 0 for full agreement, 1 for disagreements or
unavailable cases, and 2 for configuration errors. Reports default to
`evaluation/reports/latest.json` (overwritten each run, ignored by Git); they
include individual differences, assessments, model/request provenance, usage,
and processing durations. Use a different `--report` path to retain each run.

`--run` makes fresh calls in a temporary database and leaves the main database
and frozen labels unchanged. Failed calls remain missing in the evaluation;
previous successful results cannot mask them. `--prompt`, `--examples`,
`--examples-dir`, `--timeout`, and `--retries` configure fresh classification;
`--tabs-dir` changes the source directory. Full examples are omitted by default,
as in the classifier. Stored comparisons can contain results from different
prompts/models; inspect report provenance or use a fresh run for a consistent
comparison.

Most labels are **model-generated baseline drafts**, so matching them measures
stability, not independently verified accuracy. The initial offline comparison
matches by construction. `confirmed_fields` identifies your three explicit
expectations: Mertz's `length_ok=false`, the original Giuliani's
`is_complete=true`, and the synthetic Giuliani's `is_complete=false`. The complete
Giuliani is still rejected for advanced difficulty: completeness is scored
independently of acceptance. Koshkin's disputed `is_real_piece` expectation is
null and excluded from scoring; its other draft labels remain scored. Mertz's
musical-substance rejection is still a draft. Lagrima and Sarabanda are marked
as calibration cases because the prompt explicitly names them.

Review labels by editing each case's `expected` values and notes, adding reviewed
fields to `confirmed_fields`. Null means unscored. Keep acceptance consistent
with the six checks and level suitability consistent with difficulty. Labels
record hashes of decoded source text; if you intentionally change a tab, review
its expectations and update its hash rather than silently evaluating different
content against the old label. Tab files remain local and ignored by Git, so
another checkout needs the same source files to run this set.

Accepted examples:
zarate_jorge_pequena_suite_3_sarabanda.txt
tarrega_lagrima.txt

Rejected examples:
olcott-bickford_op025_p024a_buttercup_waltz.txt
barrios_la_catedral_1_prelude.txt
bach_js_bwv1004_violin_partita_no2_in_dm_5_1_chaconne_proeis_shurley_2001.txt
