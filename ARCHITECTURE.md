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
9. **Record retrieval is authorization-scoped.**
   Staging/production requests require a configured Bearer-token principal and server-side person/household/document checks. Development mode remains unrestricted and must use synthetic data.
10. **Assistant output is evidence-first.**
    The initial assistant retrieves structured records and cites stored source IDs; it does not generate clinical facts or write to canonical records.
11. **Conflicts require explicit adjudication before promotion.**
    Open and evidence-pending cross-document conflicts block disputed-field
    verification and affected event/canonical writes. A scoped reviewer
    decision preserves candidate evidence and history; resolving a conflict
    does not automatically verify any field.
