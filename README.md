# Classical guitar tab classifier

I built this to help build my classical guitar repertoire. There are so many
tabs available that finding complete pieces within my skill level is hard.
This app uses an LLM to assess tabs and help me build a
repertoire I can actually play, with a local browser for reviewing the results
and rating pieces.

Python 3.10+; no third-party packages. Classifies complete `.txt` guitar tabs using
OpenRouter or a local Strata engine, then stores results in SQLite.

## Quick start

1. Download the ZIP archive linked on [Classical Guitar Tablature](https://www.classtab.org/).
2. Create a `tabs/` folder in this repository and extract the archive into it.
   The classifier searches recursively for `.txt` files, so nested folders are fine.
3. Choose a model supporting structured JSON output, set your OpenRouter credentials
   and model, then classify one tab and browse the result:

```powershell
$env:OPENROUTER_API_KEY = 'your-key'
$env:OPENROUTER_MODEL = 'your-provider/model-id'
python classify_tabs.py tabs --limit 1
python web.py
```

Open `http://127.0.0.1:8000`. Remove `--limit 1` to classify the full collection;
API calls incur the selected model's usage charges. Local Strata setup is described below.

Tab files are downloaded separately and are ignored by Git. All rights to the
tablature and original works are reserved to their respective owners and original
authors. This project claims no ownership of those materials.

## Provider setup

For a running Strata server, select its model ID from `/v1/models`:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/models
python classify_tabs.py --provider strata --model your-local-model --limit 1
# Custom server address (include /v1)
python classify_tabs.py --provider strata --base-url http://127.0.0.1:8399/v1 --model your-local-model
```

Alternatively set `AI_PROVIDER=strata`, `STRATA_MODEL`, and optionally
`STRATA_BASE_URL` (default `http://127.0.0.1:8080/v1`). Set `STRATA_API_KEY`
only if your server requires authentication. The Strata provider never uses
OpenRouter credentials or routing parameters.

Assessments are subjective. Evaluation labels are mostly model-generated, so
agreement with them does not establish accuracy.
