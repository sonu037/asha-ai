# Document Intelligence Validation

## Verdict

**Needs improvement.** The four known defects have regression coverage and
the fixes were exercised through the upload and person-linking APIs. Tesseract
was already installed but not on `PATH`; Python successfully invoked it through
its absolute executable path. Thirteen generated synthetic scans were
processed twice through the actual upload, Tesseract, extraction, and
classification pipeline with identical OCR output and metrics. This is a small
printed-text engineering challenge set, not a representative dataset.

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
- Fixed calendar validation, ambiguous numeric date handling, overlapping
  classifier signals, and same-person/same-visit cross-document conflict
  detection. Regression tests include invalid/ambiguous date corrections,
  overlapping and absent classification signals, positive conflict cases, and
  negative cases for different dates, different people, and ambiguous encounter
  dates.
- Added
  [`tests/fixtures/real_ocr_scan_cases.json`](../tests/fixtures/real_ocr_scan_cases.json)
  and [`scripts/evaluate_real_ocr.py`](../scripts/evaluate_real_ocr.py). The
  latter generates image pixels at runtime and invokes the configured
  `TesseractOCRProvider` via the real upload endpoint. It refuses a fixture
  transcript or non-Tesseract provider.
- Added pipeline-level tests for evaluation outputs, missing and unsupported
  uploads, and persistence of an OCR failure with no fabricated facts.
- Existing tests also cover evidence filtering in the external adapter,
  Tesseract failure behavior, verification workflow, and person-scope access.

All fixture text and generated images are synthetic. Both runners use
temporary databases and document directories; they do not use the local
application database or uploaded files. The committed scan dataset is a JSON
generation manifest; generated scan images and evaluation outputs are not
committed.

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

### Generated scanned-image set

The separate real-OCR evaluation has **13** generated PNG/JPEG documents:
clear 1400x900 print, smaller font, low resolution, 3-degree skew, mild blur,
JPEG quality 35, low contrast, missing fields, ambiguous and impossible dates,
two same-person/same-visit records with conflicting blood pressures, and a
blank unreadable page. A synthetic unsupported-PDF request is also included.
The manifest records the exact text, dimensions, font size, transformations,
ground-truth type/fields/date status, and conflict-group identity for every
case. The generator uses Pillow and the locally installed Arial TrueType font;
image transforms and saved image bytes are deterministic.

These are generated printed records, not handwriting, real forms, real scans,
or real beneficiary information. Nearly all field lines were recognized
exactly. A single example per quality type cannot estimate performance for
that quality class or support a pilot decision.

## Software and provider configuration

- Environment: Windows; Python 3.14.0 in the project virtual environment.
- `pytesseract` and Pillow are installed. The executable was present at
  `C:\Program Files\Tesseract-OCR\tesseract.exe` but not discoverable as
  `tesseract` on the default `PATH`. Direct execution and Python invocation by
  absolute path both succeeded. Reported Tesseract version:
  `5.5.3.20260724`.
- No Tesseract installation or persistent system configuration change was
  performed. The OCR evaluator accepts `--tesseract-executable`, sets
  `pytesseract`'s executable path in that process only, and leaves system and
  user `PATH` unchanged. Run it using:

  ```powershell
  python -m scripts.evaluate_real_ocr `
    --tesseract-executable "C:\Program Files\Tesseract-OCR\tesseract.exe"
  ```

- The transcript evaluator uses `synthetic_evaluation_fixture` and does not
  invoke OCR. The scan evaluator used `tesseract` and generated raster image
  pixels; no transcript was injected. No live external provider was configured
  or called. At audit time, provider selection, external-processing opt-in,
  endpoint, key, and model environment settings were absent. No credential
  values were read or printed.
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
persistence routes, using only the synthetic fixture provider. These
transcript-fixture results are kept separate from the generated-scan results
below.

| Metric | Result | Denominator and calculation |
|---|---:|---|
| Classification accuracy | 8/8 = **100%** | Labeled uploads with `extraction_status=extracted`; the unreadable OCR failure is excluded and `unknown` is a valid label. |
| Exact field-value precision/recall/F1 | 18/18 / 18/18 / **100%** | Micro-averaged exact `(case, field, value)` pairs; invalid-date candidate is excluded from accepted predictions. |
| Date decision accuracy | 7/7 = **100%** | Includes exact valid values, explicit ambiguous-date flags, and impossible dates retained only as invalid review candidates. |
| Persisted source quote contained in persisted OCR text | 19/19 = **100%** | All predicted facts with a non-empty source quote; this checks literal containment, not whether the extracted field value is semantically correct. |
| Exact expected source-evidence precision/recall/F1 | 19/19 / 19/19 / **100%** | Exact normalized `(case, field, source quote)` pairs. |
| Review-required flag accuracy | 9/9 = **100%** | Labeled uploads whose persisted failure/classification/fact metadata indicated review. This measures flags, not delivery to a human queue. |
| Low-confidence warning | 1/1 = **100%** | The one fixture with supplied confidence below 60 had a persisted low-confidence warning; this was injected metadata. |
| Ambiguous-date recognition | 1/1 = **100%** | Ambiguous date candidate is explicitly labeled for locale interpretation review. |
| Unsupported PDF rejection | 1/1, HTTP **415** | One unsupported-format upload attempt. |
| Injected OCR failure | 1/9 = **11.1%** | Fixture-injected failure count only; **not** an OCR failure-rate estimate. |
| Explicit cross-document conflict flag | 1/1 groups | One labeled same-person/same-visit group; both source field references are returned and persisted. |
| Duplicate reprocessing | 0/1 replays | One byte-identical replay; it returned as a duplicate without another provider call. |

### Results by difficult-case category

The field F1 column is field-name F1, treating any output name as a predicted
field; exact value metrics above additionally penalize incorrect and unexpected
values.

| Category | Documents | Classification | Field-name F1 | Observation |
|---|---:|---:|---:|---|
| ANC | 1 | 1/1 | 1.000 | An ANC heading remains decisive despite a shared blood-pressure measurement. |
| Missing field | 1 | 1/1 | 1.000 | The two present candidates were extracted; no absent measurement was fabricated. |
| Poor-quality scan | 1 | 1/1 | 1.000 | Injected low OCR confidence had review metadata; no pixel OCR was run. |
| Ambiguous date | 1 | 1/1 | 1.000 | The string is retained and explicitly flagged as day/month-order ambiguous. |
| Impossible date | 1 | 1/1 | 1.000 | `39/19/2026` is retained only as an invalid, review-required candidate and excluded as a valid date. |
| Ambiguous document | 1 | 1/1 | 1.000 | Multiple document-type signals returned `unknown`; the explicit ANC field remained a review-required candidate. |
| Conflicting values | 2 | 2/2 | 1.000 | Linked same-person/same-visit values are explicitly flagged with both document/field/source references. |
| Unreadable scan | 1 | Excluded (0 eligible) | N/A | The fixture adapter returned extraction failure and no facts. |

The fixture runner reported **0 individual failures** after the corrections.
This remains a rule/persistence regression evaluation only and is not evidence
of OCR performance.

### Correctness fixes and regression cases

1. **Impossible dates:** `datetime.date` calendar validation now marks
   impossible dates as invalid, keeps the original candidate/source line for
   review, and does not accept it as a valid field value. Verification rejects
   invalid or still-ambiguous input; a corrected unambiguous date is accepted.
   Tests cover `39/19/2026`, leap and non-leap February 29, unambiguous dates,
   two-digit years, and correction via ISO date.
2. **Overlapping classification signals:** generic `blood pressure` was
   removed as a stand-alone NCD document-type signal because it is a shared
   field in records such as ANC. ANC with a blood-pressure field now classifies
   as ANC. Text with multiple remaining document-category signals returns
   `unknown`, sets ambiguity/review metadata, and persists an explanatory
   warning. Tests cover overlap, category-specific shared terms, and text with
   no supported category signal.
3. **Cross-document conflicts:** when documents are linked to the same person,
   the app compares same-type records with the same unambiguous visit date and,
   when available, the same ANC visit number. Differing shared field values
   create a persistent conflict row, retain both document/field/value/source
   references, and mark both field candidates for review. Tests verify the
   same-visit conflict and negative cases for another visit date, another
   person, and an ambiguous encounter date.
4. **Ambiguous dates:** slash dates such as `03/04/2026` remain in the source
   format and receive a specific day/month ambiguity reason; no locale is
   assumed. Formats with a component greater than 12 are unambiguous by
   elimination; ISO `YYYY-MM-DD` is explicit. Two-digit years require review.
   Rule-based and optional model-adapter facts both retain date review reasons.

### Actual generated-scan OCR results

Command:

```powershell
python -m scripts.evaluate_real_ocr `
  --tesseract-executable "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

All **13** runtime-generated PNG/JPEG images were uploaded through
`POST /documents`, processed by actual local Tesseract **5.5.3.20260724** and
the downstream deterministic rule pipeline, and then read back through
`GET /documents/{id}`. The app used a temporary database and upload directory.
No transcript or OCR output was injected. The same command was run twice; both
runs had identical generated-image SHA-256 values, OCR text, metric JSON,
Tesseract version, and **0 individual failures**.

| Metric | Result | Denominator and calculation |
|---|---:|---|
| Classification accuracy | 12/12 = **100%** | Successfully extracted scans only; the blank unreadable page is an OCR failure and excluded from the classifier accuracy denominator. |
| Confusion matrix | **ANC → ANC: 12**; all other cells 0 | Full class labels: `anc_record`, `asha_diary`, `home_visit`, `immunization_record`, `inventory_record`, `ncd_screening`, `pregnancy_record`, `unknown`, `vhnd_record`. Every non-ANC row has zero support in this dataset; unreadable failure is separately counted. |
| Exact field-value precision | 34/34 = **100%** | Exact `(case_id, field_name, value)` predictions divided by all accepted candidate pairs. |
| Exact field-value recall | 34/34 = **100%** | Exact expected field pairs recovered divided by all expected pairs. |
| Exact field-value F1 | **100%** | Harmonic mean of exact-pair precision and recall. |
| Date validation and ambiguity handling | 12/12 = **100%** | Ten unambiguous-date documents required exact values; the ambiguous date required a specific ambiguity warning; the impossible date required an invalid-date warning and exclusion from accepted valid-date predictions. |
| Cross-document conflict detection | TP=1, FP=0, FN=0; **100% precision/recall** | One labeled same-person/same-visit conflict pair with different blood pressures; both source references were retained. |
| Persisted evidence grounding | 34/34 = **100%** | Every emitted field's source quote was a normalized literal substring of persisted OCR text. |
| Expected text evidence found by OCR | 46/46 = **100%** | The 34 expected field source lines plus 12 `ANC Record` classification signals were found in OCR text. This is line-level recall, not character-level OCR accuracy. |
| OCR failure rate | 1/13 = **7.7%** | The intentionally blank unreadable scan failed extraction with no facts; the other twelve were extracted. This is a challenge-set rate, not an estimate for arbitrary scans. |
| Unsupported PDF | 1/1 correctly rejected, HTTP **415** | One synthetic unsupported-format upload. |

#### Results by scan-quality category

Field P/R/F1 uses exact field-value pairs. Class accuracy uses only extracted
documents; the unreadable document is correctly shown as not classifiable
because OCR failed.

| Category | n | OCR failures | Classification | Field P/R/F1 |
|---|---:|---:|---:|---:|
| Clear readable | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Small text | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Low resolution (680x440) | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Skew (3 degrees) | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Mild blur (radius 0.8) | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| JPEG compression (quality 35) | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Low contrast | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Missing fields | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Ambiguous date | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Impossible date | 1 | 0 | 1/1 | 1.00/1.00/1.00 |
| Conflicting values | 2 | 0 | 2/2 | 1.00/1.00/1.00 |
| Unreadable blank page | 1 | 1 | Not eligible (0 extracted) | N/A |

#### Error cases and OCR/downstream attribution

There were **no mismatches** in this run, so the evaluator did not assign any
observed failures to either OCR or downstream extraction/classification. The
runner distinguishes the stages by checking whether the expected text line was
present in OCR output: when present but the rule result differs, the failure is
classified downstream; when missing from OCR, it is classified as OCR or an
upstream image-recognition error. That attribution is a heuristic and does not
prove causation. The unreadable-page failure is the expected result, not a
failure case.

The generated ambiguous date `03/04/2026` remained a review-required candidate
with the explicit message that both day/month and month/day interpretations
are possible. The impossible date `39/19/2026` remained visible with its
original source line and was labeled invalid rather than accepted as a valid
date. Both conflicting blood-pressure fields remained present and were flagged
with the other document and field identifiers and their source lines.

## Safety and regression checks

- Missing upload file: HTTP 422.
- Unsupported PDF: HTTP 415 before processing.
- Injected OCR failure: persisted as `failed` with empty OCR text and no facts.
- Duplicate content: returned the existing document and did not run the
  provider a second time.
- Rule facts remained marked for review; these candidates were not treated as
  verified values.
- Invalid and ambiguous dates cannot be marked verified using the invalid or
  still-ambiguous form; tests verify that a corrected unambiguous ISO date is
  accepted.
- Cross-document conflict records preserve both document IDs, field IDs,
  values, and source quotes; negative tests cover a different visit date,
  another person, and an ambiguous visit date.
- Conflict comparison only runs when a document is linked to a person; this
  release does not backfill conflicts across records linked before deployment.
- Evidence tests verify the optional adapter omits unsupported source quotes.
- Existing authorization tests verify scoped access and cross-person denial.
- Targeted test command and result during this validation:
  `pytest tests/test_document_intelligence_evaluation.py tests/test_document_conflicts.py tests/test_real_ocr_evaluation.py tests/test_ai_intelligence.py tests/test_tesseract_ocr.py -q`
  — **28 passed**, with 3 deprecation warnings.

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
   that test set's results; do not derive them from these small generated sets.
5. Require a working human-review workflow, not only a stored boolean, before
   considering review routing complete.

## Remaining blockers and next steps

1. The existing executable is not on the standard `PATH`; either keep using the
   evaluator's process-local `--tesseract-executable` option or have an
   operator explicitly approve a persistent `PATH` update before changing
   machine configuration.
2. Build a larger, blinded, independently reviewed synthetic or appropriately
   authorized de-identified dataset with adjudicated labels, explicit conflict
   links, and per-category sample-size plans.
3. Extend conflict handling to support explicit reviewer adjudication and
   resolution; this milestone detects and preserves conflicts but does not
   provide a conflict-resolution workflow.
4. If model evaluation is desired, configure and authorize an approved
   provider, verify its data-sharing terms, and evaluate with synthetic data
   first. Do not send identifiable health records without explicit approval.
5. Implement and test an operational human-review queue and its authorization
   boundaries.

## Final validation commands

- `pytest -q` — **60 passed, 3 deprecation warnings**.
- `python -m compileall -q app tests scripts` — passed.
- `git diff --check` — passed.
- Pylance diagnostics for the real OCR evaluation runner, its tests, and
  conflict-detection module — no diagnostics.
