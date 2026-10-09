# ASHA AI — Data Dictionary v1 (MVP canonical layer)

This is the **internal canonical model**, not a claim that every state uses identical forms. State/program-specific fields belong in source mappings.

## Core entities

| Entity | Purpose | MVP fields |
|---|---|---|
| ASHA_WORKER | worker identity/context | asha_id, name, village_id, subcentre_id, block_id, district_id, state_code |
| HOUSEHOLD | stable household container | household_id, village_id, address, head_person_id |
| PERSON | person-level longitudinal identity | person_id, household_id, name, sex, date_of_birth, relationship_to_head |
| PREGNANCY | pregnancy episode | pregnancy_id, person_id, LMP, expected_delivery_date, gravida, parity, status |
| ANC_EVENT | antenatal contact | pregnancy_id, event_date, visit_no, facility, key_observations, referral |
| DELIVERY | delivery episode | pregnancy_id, date, place, mode, outcome, birth_weight |
| CHILD | child identity | child_id, person_id, birth_date, sex, birth_weight, registration_status |
| HOME_VISIT | HBNC/HBYC-style visit | person/child, visit_date, visit_number, observations, counselling, referral |
| IMMUNISATION_EVENT | vaccine event | child_id/person_id, date, vaccine, dose, facility, due_or_given |
| NCD_SCREENING | adult screening | person_id, age, sex, CBAC-related inputs, screening_date, referral |
| REFERRAL | follow-up/referral | person_id, date, reason, referred_to, status, outcome |
| INVENTORY_ITEM | drug/supply item | item_code, name, unit, opening_balance |
| INVENTORY_TRANSACTION | stock movement | item_id, date, transaction_type, quantity, balance |
| WORK_ACTIVITY | ASHA work activity | asha_id, activity_type, date, count/details |
| VHND_EVENT | village health & nutrition day | village_id, date, attendance, services, followups |
| DOCUMENT | original evidence | document_id, type, filename, language, source, captured_at, hash |
| DOCUMENT_FIELD | AI/manual fact from evidence | document_id, field_name, value, confidence, location, method, verification |
| AUDIT_LOG | traceability | actor, action, entity, timestamp, details |

## AI/provenance rules

1. A model output is **not** automatically a clinical fact.
2. Every extracted fact stores source document, extraction method, confidence and review state.
3. Original uploads remain immutable.
4. Low-confidence or conflicting facts must enter review rather than silently overwrite data.
5. Canonical entities should not store state-specific form labels as their primary identity.
6. Sensitive health fields should be minimized and access-controlled.
7. A later interoperability adapter may map canonical records to authorized government systems; the MVP does not scrape or impersonate them.

## Initial extraction priority

1. Household/person identity
2. pregnancy and ANC facts
3. delivery/newborn facts
4. child immunisation facts
5. home-visit facts
6. NCD screening
7. inventory/activity records

## Validation examples

- Dates must be parseable and should not silently become today's date.
- Sex/value vocabularies must be normalized while preserving original text.
- Child birth date must not be after a documented visit date.
- A pregnancy event should link to a person, not only a household.
- Duplicate people should trigger a match/review workflow rather than creating a second person automatically.
