# ASHA AI — MVP v0.2

AI-first, offline-capable documentation and health-record intelligence layer for ASHA/frontline health workers.

## What changed
This version moves beyond CRUD scaffolding into a vertical slice:

**source document → extraction contract → confidence/review → human verification → canonical event → household timeline**

The AI provider is intentionally a deterministic placeholder. No clinical AI claim is made until the extraction adapter is evaluated on real, appropriately governed data.

## Run

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `/docs` for the API UI.

## Important
Do not put identifiable patient records into this development build until authentication, authorization, encryption, retention, audit controls, and the applicable privacy/governance review are implemented.

See `DATA_DICTIONARY_V1.md` and `PRODUCT_ROADMAP.md` before expanding the schema.
