# ASHA AI — Architecture Principles

1. **Original evidence is immutable.**
   Scans/PDFs are preserved and never silently overwritten.

2. **AI is not the source of truth.**
   AI creates candidate structured data; human verification can promote it to trusted data.

3. **Every important fact has provenance.**
   A canonical fact should be traceable to its source document/page/field where applicable.

4. **Canonical data is domain-oriented.**
   Household, Person, Event, Pregnancy, Child, Screening, Referral, Inventory and Work Activity are modeled separately.

5. **Offline-first.**
   The eventual mobile client must work without continuous connectivity.

6. **Government-system interoperability is future work.**
   The product must not impersonate or replace official systems without authorization.

7. **Privacy by design.**
   Health/identity data requires encryption, least-privilege access, audit logging and controlled retention.

8. **AI evaluation is mandatory.**
   OCR/extraction/entity-resolution performance will be measured against a human-verified pilot dataset.
