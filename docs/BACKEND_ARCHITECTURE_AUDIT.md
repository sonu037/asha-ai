# ASHA AI Backend Architecture Completion Audit

**Audit date:** 2026-10-09
**Audited branch and baseline:** `feat/evidence-grounded-assistant` at
`236af044de090b8b8113b8e9d1ee2a8b7872f207`
**Scope:** Existing backend only. No pretrained model, embedding, VLM, fine-tuning,
or unrelated product implementation is included.

## Executive verdict

**The backend architecture is not complete.** The repository contains a working
FastAPI/SQLite vertical slice with household and person creation, image upload,
local OCR and rule extraction, evidence persistence, field/classification
verification, event creation, scoped search and retrieval, and audit records.
The code and tests verify parts of those paths. They do not establish a complete
backend lifecycle or production security/operations baseline.

The most consequential gaps are: no general document/classification review
queue; no required-field checklist or follow-up task API; incomplete
household/person lifecycle operations; unversioned in-code schema migrations;
and incomplete operational controls around configuration, health/readiness,
audit coverage, token lifecycle, backups, and deployment verification.

This is a code-and-test audit, not a security certification or privacy review.
It does not inspect the contents of the local database or uploaded files.

## Current architecture and data flow

The service is a single FastAPI application. `app/main.py` owns the primary
routes and includes the `app/batch_a.py` and `app/assistant_routes.py` routers.
SQLite access is performed directly through parameterized SQL in route and
service modules; there is no ORM or repository layer. `app/db.py` creates the
schema and applies additive migrations at startup. `app/config.py` fixes the
database and document-storage locations relative to the application directory.

The document path is:

1. `POST /documents` accepts JPEG, PNG, or WebP uploads up to 10 MiB, hashes
   bytes for duplicate detection, writes a UUID-named file, and persists its
   metadata.
2. The configured provider runs local Tesseract OCR by default, then rule-based
   classification and field extraction. An optional OpenAI-compatible
   OCR-text adapter exists but has not had live inference validated.
3. OCR text, provider/version metadata, warnings, candidate facts, source
   snippets, and review flags are persisted.
4. A reviewer can verify classification, link a document to a person, and
   verify fields. A verified, linked ANC document can be mapped to an event
   under specific gates; explicit event-creation endpoints also exist.
5. Linked document conflicts are detected for a limited set of same-person,
   same-document-type, same-visit comparisons. Scoped queue, detail and
   adjudication endpoints now preserve candidate sources and review history.
   Unresolved conflicts block disputed-field verification and relevant event
   and canonical-record promotion paths.

The core upload/verification flow is implemented in
[`app/main.py`](../app/main.py#L231), provider selection and extraction in
[`app/document_intelligence.py`](../app/document_intelligence.py#L23), and
cross-document comparison in [`app/document_conflicts.py`](../app/document_conflicts.py#L72).

## API and database inventory

### Registered API surface

| Area | Routes in the current application |
|---|---|
| Health | `GET /health` |
| Household/person | `POST /households`, `GET /households`, `GET /households/{household_id}/timeline`, `POST /people`, `GET /people/{person_id}` |
| Documents | `POST /documents`, `GET /documents/{document_id}`, `GET /documents/{document_id}/person-candidates`, `GET /documents/{document_id}/match-candidates`, `POST /documents/{document_id}/link-person`, `POST /documents/{document_id}/classification/verify`, `POST /documents/{document_id}/fields/{field_id}/verify`, `POST /documents/{document_id}/create-event`, `POST /documents/{document_id}/create-verified-event` |
| Conflict review | `GET /conflicts?status=unresolved&limit=20&offset=0`, `GET /conflicts/{conflict_id}`, `POST /conflicts/{conflict_id}/review` |
| Events and canonical records | `POST /events`, `POST /canonical-records`, `GET /people/{person_id}/records`, `GET /people/{person_id}/timeline`, `GET /people/{person_id}/pregnancy-summary` |
| Search and summary | `GET /search`, `GET /people/{person_id}/health-summary` |
| Assistant | `POST /people/{person_id}/assistant` |

Conflict listing is bounded (limit 1-100) and accepts `unresolved`, `open`,
`needs_evidence`, and the four resolved statuses. Review decisions are
`resolve_source_a`, `resolve_source_b`, `resolve_with_evidence`,
`needs_evidence`, and `not_conflict`. Route declarations are in
[`app/main.py`](../app/main.py#L55),
[`app/batch_a.py`](../app/batch_a.py#L221), and
[`app/assistant_routes.py`](../app/assistant_routes.py#L11); conflict review
routes are in [`app/conflict_routes.py`](../app/conflict_routes.py#L14).
There are no household/person update, deactivate, merge, or delete routes; no
general document review-queue route; and no checklist or follow-up-task routes.

### SQLite tables

`app/db.py` defines:

- `households`, `people`
- `documents`, `document_fields`, `document_person_links`
- `canonical_records`, `events`
- `document_field_conflicts`, `document_conflict_reviews`
- `audit_log`

Foreign keys are enabled on application connections. There are unique external
IDs, a unique document/person link, and conditionally-installed uniqueness
indexes for document hashes and event/canonical deduplication. Conflict
decisions have a separate history table. The schema has no general
task/checklist, token registry, or household/person lifecycle tables.
Definitions and additive migrations are in
[`app/db.py`](../app/db.py#L20).

## Component findings

Status definitions used below:

- **Complete and verified:** the stated narrow capability is implemented and
  supported by relevant tests/evidence.
- **Implemented but incompletely tested:** the core path exists; meaningful
  edge, operational, or deployment validation remains.
- **Partially implemented:** only a subset of the requested workflow exists.
- **Missing:** no implementation was found.
- **Blocked by an external dependency:** the code path depends on external
  approval, configuration, or software not demonstrated in this audit.

| Backend area | Status | Evidence and remaining limitation |
|---|---|---|
| Household and person lifecycle | **Partially implemented** | Creation, household listing, person/household reads, and timelines exist in `app/main.py`. There are no edit, deactivation, merge, delete, duplicate-resolution, or usable household-head assignment workflows. Validation is mostly required/type-level rather than domain-level. |
| Document upload, processing, and deduplication | **Implemented but incompletely tested** | Upload size/extension checks, SHA-256 content deduplication, stored source images, local Tesseract, extraction metadata, and failure status are implemented in `app/main.py` and `app/ocr/tesseract_provider.py`. The previous milestone exercised 13 synthetic images through the upload pipeline twice; this is not a large or representative production dataset. Upload accepts by filename suffix and the filesystem/DB/provider lifecycle is not one atomic operation. |
| Document review and verification | **Partially implemented** | Per-field and classification verification exist; unverified/ambiguous facts are flagged and the verified-event path has gates. There is no queue/list, assignment, due date, reviewer workload, reopen/reject workflow, or general review status lifecycle. |
| Database integrity and transactions | **Implemented but incompletely tested** | Each `connection()` scope commits on success, rolls back on exception, and enables SQLite foreign keys. Several uniqueness constraints and dedupe tests exist. Data-level checks for enumerated statuses, canonical value shapes, and person/household consistency are incomplete. Filesystem writes and later OCR/DB updates span separate transaction boundaries. |
| Schema migrations and persistence | **Partially implemented** | `init_db()` runs `CREATE TABLE IF NOT EXISTS` and additive column/index migrations; tests cover fresh, repeat, legacy, and duplicate-row cases. Migrations are inline and unversioned; there is no migration ledger, explicit rollback plan, backup/restore procedure, or production upgrade exercise. The helper deliberately skips uniqueness indexes when pre-existing duplicate groups exist and only emits a warning. |
| Required-field checklists | **Missing** | There is no checklist schema/API. Pregnancy summary computes a narrow set of LMP/EDD/ANC measurement completeness booleans, but the assistant explicitly reports generalized missing-field questions unavailable. |
| Follow-up task APIs | **Missing** | No task table or task routes exist. Follow-up questions return `unavailable`; there is no create/assign/due/status/complete workflow. |
| Search and longitudinal retrieval | **Partially implemented** | `/search` searches record metadata and applies person/household scoping; timeline, pregnancy summary, health summary, and structured assistant retrieval exist. The assistant uses keyword-selected intents, not general evidence-grounded question understanding. It does not provide a generalized query over OCR/source text, and incomplete workflows are returned as unavailable. |
| Cross-document conflict handling | **Implemented but incompletely tested** | Same-person comparisons across matching document type/date and optionally ANC visit create conflict rows and flag both fields for review. `GET /conflicts` is bounded and scope-filtered; detail returns candidate values, source text, document/person context and decision history; `POST /conflicts/{id}/review` records an authorized decision, rationale, reviewer and timestamp. `open` and `needs_evidence` remain blocking; source-based, external-evidence and justified not-conflict resolutions are explicit. Newly detected contradictions reopen matching resolved conflicts. Field verification, automatic/explicit document events, generic event creation and canonical-record creation consult centralized conflict rules. Discovery remains limited to matching document type and dated visits; assignment and general document/classification review queues are not implemented. |
| API schemas, validation, and errors | **Partially implemented** | Pydantic request models, query constraints, and explicit 4xx cases exist. Route responses are mostly untyped dictionaries without response models; date/status/value constraints are uneven; error response shapes are framework-default and not normalized. `create_household` catches broad exceptions and includes the underlying exception text in its 409 detail (`app/main.py`). |
| Authentication and resource authorization | **Implemented but incompletely tested** | In staging/production, bearer tokens are SHA-256 hashed and compared; principal scopes guard person, household, and document routes. Tests cover unauthenticated production access, cross-person reads/writes, actor spoofing, malformed configuration, and duplicate-upload non-disclosure. Development defaults to an unrestricted administrator. Scoped tokens are static configuration with no issuance, expiry, rotation, or revocation workflow. |
| Audit logging | **Partially implemented** | Several writes and authorized reads insert `audit_log` records; event verification and document linking have explicit audit writes. Audit events are not a complete immutable audit system: failed authentication/authorization, many failure paths, and future mutation/deletion workflows are not comprehensively captured; there is no retention, integrity protection, export, or operator review interface. |
| Configuration, logging, health, and failure operations | **Partially implemented** | Environment variables configure auth and optional document provider; OCR failures are generally represented explicitly. DB and document paths are fixed in `app/config.py`; `/health` returns static status and does not check DB/provider readiness. No structured application logging, metrics, tracing, backup policy, or documented deployment profile was found. |
| Test isolation and integration | **Implemented but incompletely tested** | The autouse test fixture redirects the database and document directory into `tmp_path`; `TestClient` exercises route-level workflows. Tests cover core CRUD creation, auth, dedupe, verification, conflicts, OCR contracts, and assistant behavior. There is no discovered CI workflow or deployment/integration environment test; `requirements.txt` does not pin versions. The current audit's complete-suite rerun result is recorded below once execution finishes. |
| External model inference | **Blocked by external approval/configuration; not a backend-completion prerequisite** | An opt-in OpenAI-compatible OCR-text adapter exists and requires explicit environment opt-in. The validation report says no live external provider was configured or exercised. This audit does not propose enabling or integrating a pretrained model. |
| OCR executable | **Complete and verified for the evaluated local path** | The prior milestone found Tesseract at an absolute Windows path and ran the generated-image upload-to-extraction evaluation. The executable was not on normal `PATH`; deployment still needs an explicit runtime check/configuration. `PRODUCT_ROADMAP.md` still says the executable is unavailable, which is stale relative to the validation report and should be reconciled. |

Relevant tests include [`tests/test_authorization.py`](../tests/test_authorization.py),
[`tests/test_batch_a.py`](../tests/test_batch_a.py),
[`tests/test_document_conflicts.py`](../tests/test_document_conflicts.py),
[`tests/test_document_intelligence_evaluation.py`](../tests/test_document_intelligence_evaluation.py),
and [`tests/test_real_ocr_evaluation.py`](../tests/test_real_ocr_evaluation.py).

## Security and data-integrity gaps

1. **Development is fail-open by default.** `ASHA_ENV` defaults to
   `development`, where authentication supplies an unrestricted administrator.
   This is documented as synthetic-data-only; deployment must set and validate
   a restrictive environment explicitly. See [`app/auth.py`](../app/auth.py#L122).
2. **Static bearer tokens have no lifecycle.** Hashed token comparison is
   implemented, but there are no expiry, rotation, revocation, per-token
   audit, or user/session management APIs.
3. **The health route is not readiness.** `/health` is public and returns
   static `ok`; it does not check database availability, OCR executable status,
   or writable storage. See [`app/main.py`](../app/main.py#L55).
4. **Audit coverage is not comprehensive.** Successful access and selected
   writes are logged. Rejected requests and operational events are not
   represented as a complete auditable security trail.
5. **File and database consistency is best-effort.** Upload bytes are written
   before the document row and OCR persistence are complete. The implementation
   removes the file on DB insert errors, but there is no durable processing
   state/retry/reconciliation mechanism for process termination between phases.
6. **Conflict review is implemented, but discovery and adjacent review
   lifecycle coverage remain bounded.** Active (`open`, `needs_evidence`)
   conflicts prevent verification of either disputed field and block document
   promotion. Shared guards cover automatic ANC event creation,
   `POST /documents/{id}/create-event`,
   `POST /documents/{id}/create-verified-event`, `POST /events` when a source
   document is supplied, and `POST /canonical-records` for person/household
   values and source fields. `POST /conflicts/{id}/review` requires an
   authorized in-scope principal, a supported state transition, a substantive
   reason, and (for evidence-based selection) another field linked to the same
   person and matching the conflict's field and visit. Decision history and
   audit rows are written in the same SQLite transaction as the decision. A
   selected source candidate must still be separately verified; unrelated
   fields are not automatically verified. Contradictory newly linked evidence
   reopens matching source-resolved conflicts and invalidates prior field
   verification. Detection still compares only matching document types and
   dated visits; it does not cover all record types, and there is no assignment
   or general document review queue.
7. **Schema/domain constraints are uneven.** SQLite foreign keys help prevent
   orphan references, but there are few `CHECK` constraints for status values,
   event type/date validity, and verification metadata; API fields use plain
   strings in multiple places.
8. **Sensitive-data operations are not production-ready.** Storage is local and
   unencrypted by this application; no retention/deletion, backup/restore,
   encryption-key management, rate limiting, or operational security
   procedures are implemented. The repository's own README cautions against
   real beneficiary data.

These are architecture/control gaps, not findings from a penetration test.

## Test coverage and observed failures

The test suite has route-level integration tests with isolated temporary
database and upload paths. It does not provide a coverage percentage, real
deployment checks, concurrent load tests, backup/restore tests, or complete
failure injection around filesystem/process interruption.

The preceding document-intelligence validation recorded **60 passed** with
**3 deprecation warnings**, plus a successful real-OCR synthetic evaluation.
For this audit, an initial quiet full-suite invocation stopped making progress
after 11 progress dots and was stopped. The full suite was rerun verbosely with
fail-fast enabled:

```powershell
python -m pytest -vv -x
```

**Audit rerun result:** **60 passed, 3 warnings, 0 failures in 22.09 seconds**;
the command exited successfully. The warnings were the Starlette/httpx
`TestClient` deprecation and FastAPI `on_event` deprecations. The initial quiet
run stall was not reproduced in the verbose rerun.

The real OCR results and their dataset limitations remain documented in
[`DOCUMENT_INTELLIGENCE_VALIDATION.md`](./DOCUMENT_INTELLIGENCE_VALIDATION.md).

### Conflict-review milestone validation

The conflict-review implementation was validated with synthetic test data.
The focused conflict suite passed **7 tests** after the final added regression
assertion for source-less canonical writes. The complete suite passed
**66 tests, 3 warnings, 0 failures in 45.04 seconds** before that test-only
assertion was added. `python -m compileall -q app tests scripts` and
`git diff --check` also completed successfully.

Two subsequent complete-suite reruns stalled during Windows
`TestClient`/AnyIO Proactor event-loop startup, before producing an assertion
failure; the targeted test at the first observed stall passed independently.
This intermittent test-runtime behavior remains noted rather than reported as
a product test failure. Warnings are the existing Starlette/httpx and FastAPI
lifespan deprecations.

## Prioritized implementation plan

The following is a backend-completion sequence. It deliberately excludes
pretrained models and later user-facing product work.

### P0 — Close safety and integrity gaps in existing flows

1. **Conflict adjudication and promotion guards are implemented in this
   milestone.** Remaining P0 review work: extend conflict discovery only
   against defined domain rules, and provide assignment plus general
   classification/document-review queues. Preserve existing authorization,
   evidence and audit requirements with regression coverage.
2. Define and enforce document processing states and recovery semantics across
   file write, DB insert, OCR, and fact persistence. Make retries/idempotency
   explicit and add tests for interruption/failure at each boundary.
3. Tighten canonical/event creation so "verified" state has consistent,
   enforceable provenance and verified source-field linkage across all write
   endpoints; validate domain dates/statuses and actor attribution.

### P1 — Complete the core record workflows

4. Specify household/person lifecycle operations (at minimum safe edits,
   deactivation, duplicate review/merge semantics, household membership/head
   consistency) and resource-scoped tests. Prefer reversible archival over
   destructive deletes where records have provenance.
5. Define data-driven required-field checklists and follow-up task lifecycle
   APIs (create, assign, due date, status transitions, completion evidence,
   authorization, audit); connect summaries to those records rather than
   hard-coded completeness flags.
6. Add versioned migrations with an explicit schema version, preflight checks,
   backup/restore runbook, and migration tests on representative prior schema
   versions. Surface skipped uniqueness migrations as an actionable operational
   condition.
7. Standardize request/response schemas, domain validation, pagination, and
   error shapes. Remove exception-detail leakage and add contract tests for
   each route family.

### P1 — Establish minimum operating/security controls

8. Make deployment configuration explicit and fail closed outside local
   development; add a supported token issuance/expiry/revocation/rotation
   mechanism or documented managed identity integration.
9. Add complete auditable events for sensitive mutations and auth outcomes,
   with retention/export/integrity policy.
10. Make database/storage locations configurable; separate liveness from
    readiness checks; add structured logs, provider/storage health reporting,
    and documented backup/restore and retention controls.

### P2 — Repeatable delivery and evidence

11. Add CI that installs the documented pinned environment, runs the full test
    suite and compilation checks, and validates migration/setup paths.
12. Add integration tests against the intended deployment configuration,
    including auth scopes, dependency-unavailable states, upload retry/recovery,
    and representative concurrent duplicate submissions. Track coverage only
    after defining meaningful critical-path requirements.
13. Reconcile stale setup/roadmap text, including the Tesseract availability
    note, and publish the tested runtime/configuration matrix.

### Explicitly later, not a backend-completion blocker

- Pretrained LLM, embedding, VLM, or fine-tuning integration.
- Mobile UI, camera/voice experience, offline synchronization, and
  government-system interoperability.
- Broad clinical rules or clinical decision support.

## Acceptance criteria for declaring the backend architecture complete

The backend can be declared **architecturally complete for its agreed MVP
scope** only after all of the following are evidenced:

1. Household/person create, read, update, deactivation, duplicate review, and
   safe association changes have documented semantics and scoped integration
   tests; no orphan or silently reassigned longitudinal records occur.
2. Document upload/process/retry/deduplication has a durable state machine,
   recoverable filesystem/database behavior, immutable source preservation,
   and tests for provider failure and interruption. Unsupported/unreadable
   documents fail explicitly without fabricated verified data.
3. A reviewer can find, assign, verify, reject, and resolve every pending
   classification/field/conflict item; transitions and source evidence are
   auditable; unresolved conflicts cannot be promoted as settled facts.
4. Required-field definitions and follow-up task APIs have validated
   authorization, due/status lifecycle, provenance, and tests; summaries report
   configured rather than hard-coded completeness.
5. All sensitive routes have an explicit response/request contract, validated
   domain inputs, consistent errors, resource-scope tests, and no
   caller-controlled verification attribution.
6. Production/staging cannot accidentally run with the unrestricted
   development principal; token lifecycle/revocation and security audit
   expectations are implemented and tested.
7. Schema changes use a versioned migration path tested from a fresh DB and
   supported prior versions; backup/restore and persistence recovery have a
   rehearsed procedure.
8. Health/readiness distinguish process, DB, storage, and configured OCR
   dependency; logs and audit records provide actionable, privacy-conscious
   operational evidence; retention and deletion policies are documented.
9. A clean environment can follow reproducible setup instructions and pass CI
   tests, compilation, and migration checks; supported dependency versions and
   external runtime prerequisites are explicit.
10. Security/privacy review and data-governance approvals are complete before
    real identifiable health data is used. This is separate from passing
    software tests.

Passing these criteria would establish a bounded backend foundation, not
production readiness, clinical validation, or permission to process health
data. Those require separate operational, security, privacy, and governance
gates.
