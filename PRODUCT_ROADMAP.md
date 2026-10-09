# ASHA AI Product Roadmap

## V0.3 — in progress: safe intelligence foundation
- Evidence-grounded, structured questions over authorized person records
- Chronological record ordering and source-linked comparisons of verified visit fields
- Scoped Bearer authentication for staging/production; development remains intentionally open
- Optional OpenAI-compatible OCR-text extraction adapter; requires explicit external-processing opt-in
- Synthetic classifier, extractor, retrieval, authorization, and provider-contract tests
- Remaining: install/evaluate OCR runtime, governed/de-identified extraction evaluation, token lifecycle, UI, stronger privacy/security operations

## V0.2 — current vertical slice
- Canonical entities and data dictionary
- Original-document storage
- Extraction envelope with confidence/review flags
- Human verification workflow
- Household timeline
- Audit trail

## V0.3 — real document intelligence
- OCR adapter (printed + handwriting evaluation; Tesseract runtime not yet available on the current development machine)
- page/region coordinates
- document classification (keyword rules locally; optional remote-model adapter is not yet live-tested)
- Hindi/English first; language metadata retained for future Urdu/Kashmiri support
- field normalization + validation (basic rule checks only)
- duplicate/conflict detection

## V0.4 — field workflow
- Android-first offline UI
- camera capture
- voice note → transcription → structured facts
- simple tap-based confirmation
- local encrypted queue + sync

## V0.5 — longitudinal intelligence
- pregnancy/child timelines
- due/overdue follow-up engine
- referral tracking
- stock alerts
- search by person/household

## V0.6 — pilot/evaluation
- real de-identified records
- extraction precision/recall
- field verification time
- correction rate
- duplicate detection precision
- sync reliability
- usability with representative ASHA users

## V1 — authorized interoperability
Only after product, privacy, security and governance are mature. Integrations must use documented/authorized interfaces and never impersonate government applications.
