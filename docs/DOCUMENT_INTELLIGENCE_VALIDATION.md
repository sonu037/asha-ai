# Document Intelligence Validation

## Verdict

**Blocked by missing dependencies** for validation of actual scan OCR. The
Tesseract executable was not found on `PATH`, so no real image-to-text run was
possible. No live model provider was configured or called. The controlled
synthetic evaluation did exercise the actual document upload and persistence
routes, but its fixture adapter supplied the transcript directly and cannot
establish OCR or model performance.

This MVP is **not production-ready or clinically validated**. The current
results are a small engineering check for rule behavior, evidence persistence,
uncertainty flags, duplicate uploads, and safe failures—not evidence of
performance on real-world documents.

## What was tested

- Audited the upload path in `app/main.py`, deterministic rules in
  `app/classification.py` and `app/extraction.py`, the Tesseract adapter, the
  optional OpenAI-compatible adapter, the assistant, authentication, and
  related tests.
- Added [`tests/fixtures/document_intelligence_eval.json`](../tests/fixtures/document_intelligence_eval.json),
  a nine-document, synthetic-only manifest with expected types, fields,
  source quotes, review outcomes, and difficult-case labels. It covers ANC
  records, missing fields, a low-confidence fixture, ambiguous and impossible
  dates, ambiguous classification, conflicting records, unreadable OCR, and a
  byte-identical duplicate replay. A separate synthetic PDF upload exercises
  unsupported-type rejection.
- Added
  [`scripts/evaluate_document_intelligence.py`](../scripts/evaluate_document_intelligence.py).
  It creates temporary PNGs, posts each through `POST /documents`, reads back
  persisted data through `GET /documents/{id}`, and emits JSON metrics and
  individual failures. Its synthetic adapter uses the labeled transcript,
  then calls the existing classifier and field-rule extractor. It is explicitly
  not an OCR or model provider.
- Added pipeline-level tests for evaluation outputs, missing and unsupported
  uploads, and persistence of an OCR failure with no fabricated facts.
- Existing tests also cover evidence filtering in the external adapter,
  Tesseract failure behavior, verification workflow, and person-scope access.

All fixture text and images generated during evaluation are synthetic. The
runner uses a temporary database and document directory; it does not use the
local application database or uploaded files.

## Dataset composition and limitations

There are **9** labeled image fixtures: 8 with supplied synthetic OCR text and
1 with an injected unreadable-OCR failure. The 8 transcript cases include one
ordinary ANC record, one record with missing measurements, one low-confidence
case, one ambiguous date, one impossible date, one ambiguous document type, and
two records in one conflicting-value group. One additional PDF request tests
unsupported upload rejection, and the ordinary ANC image is replayed once to
test duplicate handling.

These are curated contract cases, not a random or representative sample. Their
small size means percentages are descriptive only; they do not estimate
production performance. The low-confidence score and unreadable result are
injected fixture metadata, not measured from pixels. The conflict group is
synthetic and is not linked to a real person.

## Software and provider configuration

- Environment: Windows; Python 3.14.0 in the project virtual environment.
- `pytesseract` and Pillow are Python packages, but `tesseract` was not
  discoverable as an executable on `PATH` during the audit.
- The app's normal default is local Tesseract plus deterministic rules. For
  actual document OCR on Windows, install Tesseract from a trusted source,
  add its install directory (commonly
  `C:\Program Files\Tesseract-OCR`) to `PATH`, open a new terminal, and verify
  with:

  ```powershell
  tesseract --version
  ```

  Do not install system software without authorization. The existing
  [README OCR instructions](../README.md#ocr-runtime-requirement) show the
  temporary PowerShell `PATH` command.
- The evaluation's provider was `synthetic_evaluation_fixture`; it invoked
  neither the Tesseract executable nor a live external provider. At audit time,
  provider-selection, external-processing opt-in, endpoint, key, and model
  environment settings were all absent. No credential values were read or
  printed.
- The rule classifier is keyword-based and returns uncalibrated scores capped
  at 0.5; all classifications require review. The field extractor uses regular
  expressions and source lines from its input text; all rule facts require
  review. These are rule-based candidates, not model-backed facts.
- The optional OpenAI-compatible adapter is opt-in. It was not live-tested.
  Unit tests mock its HTTP client and test source-quote validation; those tests
  do not demonstrate external inference.
- The assistant uses structured database retrieval rather than a language
  model. Human-review flags are persisted, but this audit found no operational
  review queue.

## Measured results

Command:

```powershell
python -m scripts.evaluate_document_intelligence
```

The runner's **9** image uploads were sent through the real upload and
persistence routes, using only the synthetic fixture provider.

| Metric | Result | Denominator and calculation |
|---|---:|---|
| Classification accuracy | 5/8 = **62.5%** | Labeled uploads with `extraction_status=extracted`; the unreadable OCR failure is excluded and `unknown` is a valid label. |
| Exact field-value precision | 18/19 = **94.7%** | Micro-averaged exact `(case, field, value)` matches divided by all predicted pairs; the impossible date is one false positive. |
| Exact field-value recall | 18/18 = **100%** | Exact expected pairs found divided by all expected pairs. |
| Exact field-value F1 | **97.3%** | Harmonic mean of the preceding exact-pair precision and recall. |
| Date decision accuracy | 6/7 = **85.7%** | Date test cases; an expected date must match exactly, and an impossible date expected to be rejected must not be emitted. |
| Persisted source quote contained in persisted OCR text | 19/19 = **100%** | All predicted facts with a non-empty source quote; this checks literal containment, not whether the extracted field value is semantically correct. |
| Exact expected source-evidence precision | 18/19 = **94.7%** | Exact normalized `(case, field, source quote)` pairs divided by predicted evidence pairs. |
| Exact expected source-evidence recall | 18/18 = **100%** | Exact expected evidence pairs found divided by expected evidence pairs. |
| Exact expected source-evidence F1 | **97.3%** | Harmonic mean of exact-evidence precision and recall. |
| Review-required flag accuracy | 9/9 = **100%** | Labeled uploads whose persisted failure/classification/fact metadata indicated review. This measures flags, not delivery to a human queue. |
| Low-confidence warning | 1/1 = **100%** | The one fixture with supplied confidence below 60 had a persisted low-confidence warning; this was injected metadata. |
| Ambiguous-date recognition | 0/1 = **0%** | Generic review was present, but no specific ambiguity explanation was persisted. |
| Unsupported PDF rejection | 1/1, HTTP **415** | One unsupported-format upload attempt. |
| Injected OCR failure | 1/9 = **11.1%** | Fixture-injected failure count only; **not** an OCR failure-rate estimate. |
| Explicit cross-document conflict flag | 0/1 groups | One labeled conflict group; generic review flags were not counted as explicit conflict detection. |
| Duplicate reprocessing | 0/1 replays | One byte-identical replay; it returned as a duplicate without another provider call. |
| Ambiguous-date identification | 0/1 cases | A generic review flag does not count as recognizing the specific locale ambiguity. |

### Results by difficult-case category

The field F1 column is field-name F1, treating any output name as a predicted
field; exact value metrics above additionally penalize incorrect and unexpected
values.

| Category | Documents | Classification | Field-name F1 | Observation |
|---|---:|---:|---:|---|
| ANC | 1 | 0/1 | 1.000 | ANC plus blood-pressure terms matched multiple rule categories, so the classifier returned `unknown`. |
| Missing field | 1 | 1/1 | 1.000 | The two present candidates were extracted; no absent measurement was fabricated. |
| Poor-quality scan | 1 | 1/1 | 1.000 | Injected low OCR confidence had review metadata; no pixel OCR was run. |
| Ambiguous date | 1 | 1/1 | 1.000 | The string was retained, but its locale ambiguity was not specifically identified. |
| Impossible date | 1 | 1/1 | 0.667 | `39/19/2026` was emitted as a `visit_date` candidate instead of being rejected. |
| Ambiguous document | 1 | 1/1 | 1.000 | Multiple document-type signals returned `unknown`; the explicit ANC field remained a review-required candidate. |
| Conflicting values | 2 | 0/2 | 1.000 | ANC and blood-pressure terms made the type `unknown`; conflicting blood-pressure values were not explicitly surfaced as a conflict. |
| Unreadable scan | 1 | Excluded (0 eligible) | N/A | The fixture adapter returned extraction failure and no facts. |

### Individual failures

1. `anc_basic`: expected `anc_record`; classifier returned `unknown` because
   both ANC and NCD keywords were present.
2. `invalid_date`: emitted `visit_date=39/19/2026`, an impossible calendar
   value, as a candidate.
3. `conflict_visit_one` and `conflict_visit_two`: expected ANC classification;
   each returned `unknown` because the blood-pressure keyword triggered
   multiple classifier groups.
4. `same_visit_conflict`: different blood-pressure values (`110/70` and
   `150/90`) were not explicitly identified by conflict-specific metadata.
5. `ambiguous_date`: the date string was extracted and review-required, but no
   evidence identified its day/month versus month/day ambiguity.

## Safety and regression checks

- Missing upload file: HTTP 422.
- Unsupported PDF: HTTP 415 before processing.
- Injected OCR failure: persisted as `failed` with empty OCR text and no facts.
- Duplicate content: returned the existing document and did not run the
  provider a second time.
- Rule facts remained marked for review; these candidates were not treated as
  verified values.
- Evidence tests verify the optional adapter omits unsupported source quotes.
- Existing authorization tests verify scoped access and cross-person denial.
- Targeted test command and result during this validation:
  `pytest tests/test_document_intelligence_evaluation.py tests/test_ai_intelligence.py tests/test_tesseract_ocr.py -q`
  — **20 passed**, with 3 deprecation warnings.
- Full regression command: `pytest -q` — **52 passed**, with 3 deprecation
  warnings (Starlette/httpx TestClient and FastAPI `on_event`).
- Compilation command: `python -m compileall -q app tests scripts` — passed.
- Pylance diagnostics for the new evaluator and its tests — no diagnostics.
- `git diff --check` — passed before staging; the staged diff is checked again
  before commit.

## Recommended acceptance gates before any pilot

These are proposed safety/process gates for review and pre-registration, not
thresholds inferred from this small evaluation:

1. Require **100% exact evidence matching** for every retained candidate
   field; reject candidates whose quote is absent or mismatched.
2. Require **100% safe handling** of the predeclared unreadable, impossible
   date, unsupported-type, and cross-document conflict challenge cases:
   explicit failure/rejection or clearly identified human review, with no
   automatic verification.
3. Require **100% idempotency** for byte-identical uploads and preserve access
   checks for duplicate retrieval.
4. Before a larger, blinded, representative evaluation, have clinical,
   privacy, and product owners set and document per-document-type minimum
   classification and field precision/recall targets, minimum subgroup sample
   sizes, and confidence-interval rules. Freeze those targets before examining
   that test set's results; do not derive them from these nine cases.
5. Require a working human-review workflow, not only a stored boolean, before
   considering review routing complete.

## Remaining blockers and next steps

1. Install and verify Tesseract only with operator approval, then repeat the
   upload evaluation using real generated scans with varied text, layout, and
   image quality. Keep the transcript-only evaluation separately labeled.
2. Build a larger, blinded, independently reviewed synthetic or appropriately
   authorized de-identified dataset with adjudicated labels, explicit conflict
   links, and per-category sample-size plans.
3. Address impossible-date validation, overlapping classifier categories, and
   explicit cross-document conflict detection before evaluating them again.
4. If model evaluation is desired, configure and authorize an approved
   provider, verify its data-sharing terms, and evaluate with synthetic data
   first. Do not send identifiable health records without explicit approval.
5. Implement and test an operational human-review queue and its authorization
   boundaries.

## Final validation commands

- `pytest -q` — **52 passed, 3 deprecation warnings**.
- `python -m compileall -q app tests scripts` — passed.
- `git diff --check` and staged-diff check — passed before commit.
