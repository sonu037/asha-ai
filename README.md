# ASHA AI — MVP v0.2

AI-first, offline-capable documentation and health-record intelligence layer for ASHA/frontline health workers.

## What changed
This version moves beyond CRUD scaffolding into a vertical slice:

**source document → extraction contract → confidence/review → human verification → canonical event → household timeline**

Local classification and field rules remain conservative, review-required
baselines; an optional remote adapter is documented below. No clinical AI
claim is made until the extraction adapter is evaluated on real, appropriately
governed data.

## Run

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `/docs` for the API UI.

## OCR runtime requirement

`pytesseract` is a Python wrapper; it does not install the Tesseract OCR
executable. Install Tesseract separately using a trusted Windows installer or
package source. If it is installed in the common location
`C:\Program Files\Tesseract-OCR`, add that directory to the current PowerShell
session's `PATH` and verify the executable:

```powershell
$env:Path += ";C:\Program Files\Tesseract-OCR"
tesseract --version
```

If installed elsewhere, use that directory instead. Restart the terminal or
VS Code after making a permanent `PATH` change. If the executable is unavailable
or OCR returns no text, document extraction is marked `failed` and no extracted
fields are generated.

The upload API currently accepts JPEG, PNG, and WebP images up to 10 MiB. PDF
and HEIC conversion are not implemented in this OCR path.

## Synthetic document-intelligence evaluation

Run the controlled synthetic evaluation from the repository root:

```powershell
python -m scripts.evaluate_document_intelligence
```

It generates temporary synthetic images, submits them through `POST /documents`,
and scores the persisted results. The fixture OCR adapter supplies labeled
synthetic transcripts; it does **not** run OCR or call a model. Results measure
the upload, rule-classification/extraction, evidence-persistence, review-flag,
and duplicate-handling paths, not real scan-reading performance. See
[`docs/DOCUMENT_INTELLIGENCE_VALIDATION.md`](docs/DOCUMENT_INTELLIGENCE_VALIDATION.md)
for the measured results and limitations.

For the separate generated-image evaluation, use the actual local Tesseract
executable (this changes `pytesseract` configuration in that process only):

```powershell
python -m scripts.evaluate_real_ocr `
  --tesseract-executable "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

The runner submits generated image pixels through `POST /documents`; it refuses
to substitute fixture transcripts if Tesseract cannot be executed. It uses a
temporary database and storage directory and leaves the machine's `PATH`
unchanged.

## Document-intelligence providers

The default provider is local Tesseract OCR plus deterministic field rules.
This is not model inference. The `mock` provider is a no-op test/development
provider and does not produce OCR text or facts.
The rule classifier reports a conservative, uncalibrated score and always
requires review; rule-extracted fields are capped at `0.5` confidence and also
require human verification. These values are workflow indicators, not measured
probabilities. Source text, provider/model/version, warnings, and review flags
are persisted alongside candidate fields.

An optional OpenAI-compatible chat-completions adapter can classify OCR text
and propose structured fields. Configure all of the following in the process
environment to opt in:

```text
ASHA_DOCUMENT_PROVIDER=openai_compatible
ASHA_ALLOW_EXTERNAL_AI=true
ASHA_AI_BASE_URL=https://your-approved-provider.example/v1
ASHA_AI_API_KEY=<secret-from-your-secret-manager>
ASHA_AI_MODEL=<approved-model-id>
```

The adapter first runs local Tesseract and sends the resulting OCR text to the
configured endpoint over HTTPS. Do not enable this unless the provider,
processing terms, data location, and sharing of the documents have been
approved for the data involved. AI facts without a verbatim quote found in the
OCR text are omitted; accepted AI facts are always marked for human review.
Model-reported confidence is not calibrated clinical confidence. No live
external model call has been tested in this project.

## Authentication and access scopes

`ASHA_ENV` defaults to `development`. Development mode deliberately provides
an unrestricted local development principal and has no login; use synthetic
data only. `staging` and `production` require a Bearer token whose SHA-256
digest is listed in `ASHA_AUTH_TOKENS_JSON`. The configured subject is used for
write attribution instead of caller-supplied actor names. ASHA principals are
restricted to configured `person_ids` and/or `household_ids`; administrators
have unrestricted record access.

Example configuration (replace the example digest; never store a plaintext
token in this JSON):

```json
[
  {
    "token_sha256": "<64-character-lowercase-sha256-digest>",
    "subject": "asha-worker-001",
    "role": "asha",
    "person_ids": [12, 13],
    "household_ids": [4],
    "can_create_households": false
  }
]
```

Generate a high-entropy token and its digest locally, store the token only in
the authorized client's secret store, and put only the digest in the server's
`ASHA_AUTH_TOKENS_JSON` configuration:

```powershell
python -c "import hashlib,secrets; token=secrets.token_urlsafe(32); print('TOKEN='+token); print('SHA256='+hashlib.sha256(token.encode()).hexdigest())"
```

The API does not issue, expire, or rotate tokens; operators must provision,
protect, and revoke them out of band. Successful reads of people, households,
documents, and search are audit-logged. Health data is never returned to an
unauthenticated caller in `staging` or `production`; `/health` remains public.
This configuration is an initial access boundary, not a complete identity
platform: it lacks automated token lifecycle management, MFA, rate limiting,
encryption-at-rest management, formal role governance, and independent
security review. Do not expose this MVP to real beneficiaries.

## Evidence-grounded assistant

`POST /people/{person_id}/assistant` retrieves stored events, linked source
documents, review status, or changes between two verified visits. It uses
structured database retrieval, not a language model or unrestricted SQL.
Responses include source IDs and verification status. Missing-field questions
and follow-up-task questions return unavailable because this version has no
required-field checklist or follow-up task table; the assistant will not infer
those answers. Chronological differences compare verified values from
person-linked source documents and do not apply clinical thresholds.
Documents classified by local rules or the optional model begin in
classification-review status. Reviewers must use
`POST /documents/{document_id}/classification/verify` before those documents
can create verified events.

## Important
The development mode is intentionally unauthenticated. Production/staging
configuration adds bearer authentication, record scopes, and read audit
events, but does not constitute production security or privacy readiness.
Encryption, retention controls, operational monitoring, formal identity
management, and applicable privacy/governance review remain necessary before
handling identifiable patient records.

See `DATA_DICTIONARY_V1.md` and `PRODUCT_ROADMAP.md` before expanding the schema.
