import difflib
import json
import sqlite3
import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from .db import connection
from .models import DocumentEventCreate


router = APIRouter()


def _normalize(value):
    return " ".join(str(value or "").split()).casefold()


def _document_fields(conn, document_id):
    rows = conn.execute(
        """SELECT field_name, extracted_value, verified_value, needs_review
           FROM document_fields WHERE document_id=? ORDER BY id""",
        (document_id,),
    ).fetchall()
    fields = {}
    for row in rows:
        chosen_value = (
            row["verified_value"]
            if row["verified_value"] is not None
            else row["extracted_value"]
        )
        fields[row["field_name"]] = {
            "extracted_value": row["extracted_value"],
            "verified_value": row["verified_value"],
            "value": chosen_value,
            "needs_review": bool(row["needs_review"]),
        }
    return fields


def _escaped_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _duplicate_event(conn, document_id, person_id, event_type, event_date, details_json):
    return conn.execute(
        """SELECT id FROM events
           WHERE source_document_id=? AND person_id=? AND event_type=?
             AND COALESCE(event_date, '')=COALESCE(?, '')
             AND COALESCE(details_json, '')=?""",
        (document_id, person_id, event_type, event_date, details_json),
    ).fetchone()


def _record_event_review(conn, document_id, person_id, actor_id, reason):
    conn.execute(
        """INSERT INTO audit_log (
               actor_id, action, entity_type, entity_id, details_json
           ) VALUES (?, ?, ?, ?, ?)""",
        (
            actor_id,
            "document_event_review_required",
            "document",
            str(document_id),
            json.dumps(
                {
                    "person_id": person_id,
                    "reason": reason,
                },
                sort_keys=True,
            ),
        ),
    )
    return {"status": "review_required", "reason": reason}


def auto_create_event_from_verified_document(conn, document_id, verified_by):
    """Create an ANC visit only when its person, event number, date, and fields are verified."""
    document = conn.execute(
        "SELECT * FROM documents WHERE id=?", (document_id,)
    ).fetchone()
    if not document or document["verification_status"] != "verified":
        return {"status": "not_ready"}

    link = conn.execute(
        "SELECT person_id FROM document_person_links WHERE document_id=?",
        (document_id,),
    ).fetchone()
    if not link:
        return _record_event_review(
            conn, document_id, None, verified_by, "Document has no person link."
        )

    if document["document_type"] != "anc_record":
        return _record_event_review(
            conn,
            document_id,
            link["person_id"],
            verified_by,
            "Automatic event mapping is supported only for ANC records.",
        )

    fields = _document_fields(conn, document_id)
    unverified_fields = conn.execute(
        """SELECT COUNT(*) AS n FROM document_fields
           WHERE document_id=? AND (verified_value IS NULL OR needs_review=1)""",
        (document_id,),
    ).fetchone()["n"]
    if unverified_fields:
        return {"status": "not_ready"}

    verified_values = {
        name: field["verified_value"]
        for name, field in fields.items()
        if field["verified_value"] not in (None, "")
    }
    if not verified_values.get("anc_visit"):
        return _record_event_review(
            conn,
            document_id,
            link["person_id"],
            verified_by,
            "A verified ANC visit number is required.",
        )
    event_date = next(
        (
            verified_values[name]
            for name in ("visit_date", "event_date", "date")
            if verified_values.get(name)
        ),
        None,
    )
    if not event_date:
        return _record_event_review(
            conn,
            document_id,
            link["person_id"],
            verified_by,
            "A verified visit date is required.",
        )

    details = dict(verified_values)
    details.update({"source": "verified_document", "document_id": document_id})
    details_json = json.dumps(details, sort_keys=True, separators=(",", ":"))
    duplicate = _duplicate_event(
        conn,
        document_id,
        link["person_id"],
        "anc_visit",
        event_date,
        details_json,
    )
    if duplicate:
        return {"status": "duplicate", "event_id": duplicate["id"]}

    person = conn.execute(
        "SELECT household_id FROM people WHERE id=?",
        (link["person_id"],),
    ).fetchone()
    if not person:
        return _record_event_review(
            conn,
            document_id,
            link["person_id"],
            verified_by,
            "Linked person no longer exists.",
        )
    try:
        cursor = conn.execute(
            """INSERT INTO events (
                   external_id, household_id, person_id, event_type, event_date,
                   details_json, source_document_id, verification_status,
                   verified_by, verified_at
               ) VALUES (?, ?, ?, 'anc_visit', ?, ?, ?, 'verified', ?, CURRENT_TIMESTAMP)""",
            (
                f"DOC-EVENT-{document_id}-{uuid.uuid4().hex[:10].upper()}",
                person["household_id"],
                link["person_id"],
                event_date,
                details_json,
                document_id,
                verified_by,
            ),
        )
    except sqlite3.IntegrityError:
        duplicate = _duplicate_event(
            conn,
            document_id,
            link["person_id"],
            "anc_visit",
            event_date,
            details_json,
        )
        if not duplicate:
            raise
        return {"status": "duplicate", "event_id": duplicate["id"]}
    event_id = cursor.lastrowid
    conn.execute(
        """INSERT INTO audit_log (
               actor_id, action, entity_type, entity_id, details_json
           ) VALUES (?, ?, ?, ?, ?)""",
        (
            verified_by,
            "auto_create_event",
            "event",
            str(event_id),
            json.dumps(
                {"document_id": document_id, "person_id": link["person_id"]},
                sort_keys=True,
            ),
        ),
    )
    return {"status": "created", "event_id": event_id}


@router.post("/documents/{document_id}/create-verified-event")
def create_verified_event(document_id: int, payload: DocumentEventCreate):
    """Create a human-verified event from a verified, person-linked document."""
    if not payload.verified_by.strip():
        raise HTTPException(422, "verified_by is required")

    fields = None
    event_type = None
    event_date = payload.event_date
    details_json = None
    try:
        with connection() as conn:
            document = conn.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            if not document:
                raise HTTPException(404, "Document not found")
            if document["verification_status"] != "verified":
                raise HTTPException(
                    409,
                    "Document must be fully verified before creating an event",
                )

            unverified = conn.execute(
                """SELECT COUNT(*) AS n FROM document_fields
                   WHERE document_id=? AND (verified_value IS NULL OR needs_review=1)""",
                (document_id,),
            ).fetchone()["n"]
            if unverified:
                raise HTTPException(
                    409,
                    "All extracted fields must be verified before creating an event",
                )

            link = conn.execute(
                "SELECT person_id FROM document_person_links WHERE document_id=?",
                (document_id,),
            ).fetchone()
            if not link or link["person_id"] != payload.person_id:
                raise HTTPException(409, "Document is not linked to this person")

            fields = _document_fields(conn, document_id)
            verified_field_values = {
                name: item["verified_value"]
                for name, item in fields.items()
                if item["verified_value"] not in (None, "")
            }
            if not verified_field_values and not payload.details:
                raise HTTPException(
                    422,
                    "At least one verified field or explicit event detail is required",
                )
            details = dict(payload.details)
            for name, value in verified_field_values.items():
                if name not in details:
                    details[name] = value
            details["source"] = "verified_document"
            details["document_id"] = document_id

            if event_date is None:
                for field_name in ("event_date", "visit_date", "date"):
                    item = fields.get(field_name)
                    if item and item["verified_value"]:
                        event_date = item["verified_value"]
                        break
            event_type = (
                payload.event_type.strip()
                or document["document_type"]
                or "document_event"
            )
            details_json = json.dumps(details, sort_keys=True, separators=(",", ":"))
            duplicate = _duplicate_event(
                conn,
                document_id,
                payload.person_id,
                event_type,
                event_date,
                details_json,
            )
            if duplicate:
                return {
                    "status": "duplicate",
                    "event_id": duplicate["id"],
                    "document_id": document_id,
                    "person_id": payload.person_id,
                }

            person = conn.execute(
                "SELECT household_id FROM people WHERE id=?",
                (payload.person_id,),
            ).fetchone()
            if not person:
                raise HTTPException(404, "Person not found")
            cursor = conn.execute(
                """INSERT INTO events (
                       external_id, household_id, person_id, event_type, event_date,
                       details_json, source_document_id, verification_status,
                       verified_by, verified_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'verified', ?, CURRENT_TIMESTAMP)""",
                (
                    f"DOC-EVENT-{document_id}-{uuid.uuid4().hex[:10].upper()}",
                    person["household_id"],
                    payload.person_id,
                    event_type,
                    event_date,
                    details_json,
                    document_id,
                    payload.verified_by,
                ),
            )
            event_id = cursor.lastrowid
            conn.execute(
                """INSERT INTO audit_log (
                       actor_id, action, entity_type, entity_id, details_json
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    payload.verified_by,
                    "auto_create_event",
                    "event",
                    str(event_id),
                    json.dumps(
                        {"document_id": document_id, "person_id": payload.person_id},
                        sort_keys=True,
                    ),
                ),
            )
    except sqlite3.IntegrityError:
        with connection() as conn:
            duplicate = _duplicate_event(
                conn,
                document_id,
                payload.person_id,
                event_type,
                event_date,
                details_json,
            )
        if not duplicate:
            raise
        return {
            "status": "duplicate",
            "event_id": duplicate["id"],
            "document_id": document_id,
            "person_id": payload.person_id,
        }

    return {
        "status": "created",
        "event_id": event_id,
        "document_id": document_id,
        "person_id": payload.person_id,
        "event_type": event_type,
        "event_date": event_date,
        "verification_status": "verified",
        "verified_by": payload.verified_by,
        "source_document_id": document_id,
    }


@router.get("/search")
def unified_search(
    q: str = Query(..., min_length=2),
    limit: int = Query(20, ge=1, le=100),
    person_id: int | None = Query(None, ge=1),
    household_id: int | None = Query(None, ge=1),
    record_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """Search record metadata across people, documents, canonical records, and events."""
    query = _normalize(q)
    if len(query) < 2:
        raise HTTPException(422, "Search query must contain at least two non-space characters")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be later than date_to")
    pattern = f"%{_escaped_like(query)}%"
    with connection() as conn:
        people_where = ["(LOWER(p.name) LIKE ? ESCAPE '\\' OR LOWER(p.external_id) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(h.village_id, '')) LIKE ? ESCAPE '\\')"]
        people_params = [pattern, pattern, pattern]
        if record_type and record_type != "person":
            people_where.append("1=0")
        if person_id is not None:
            people_where.append("p.id=?")
            people_params.append(person_id)
        if household_id is not None:
            people_where.append("p.household_id=?")
            people_params.append(household_id)
        if date_from:
            people_where.append("date(p.created_at) >= date(?)")
            people_params.append(date_from.isoformat())
        if date_to:
            people_where.append("date(p.created_at) <= date(?)")
            people_params.append(date_to.isoformat())
        people = conn.execute(
            f"""SELECT p.id, p.name, p.external_id, p.household_id,
                       h.village_id, p.created_at
                FROM people p LEFT JOIN households h ON h.id=p.household_id
                WHERE {' AND '.join(people_where)}
                ORDER BY p.id DESC LIMIT ?""",
            (*people_params, limit),
        ).fetchall()

        doc_where = [
            """(LOWER(COALESCE(d.original_filename, '')) LIKE ? ESCAPE '\\'
                OR LOWER(COALESCE(d.document_type, '')) LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM document_person_links sl
                    JOIN people sp ON sp.id=sl.person_id
                    WHERE sl.document_id=d.id
                      AND LOWER(sp.name) LIKE ? ESCAPE '\\'
                ))"""
        ]
        doc_params = [pattern, pattern, pattern]
        if record_type:
            doc_where.append("d.document_type=?")
            doc_params.append(record_type)
        if person_id is not None:
            doc_where.append(
                "EXISTS (SELECT 1 FROM document_person_links l WHERE l.document_id=d.id AND l.person_id=?)"
            )
            doc_params.append(person_id)
        if household_id is not None:
            doc_where.append(
                """EXISTS (
                    SELECT 1 FROM document_person_links l
                    JOIN people lp ON lp.id=l.person_id
                    WHERE l.document_id=d.id AND lp.household_id=?
                )"""
            )
            doc_params.append(household_id)
        if date_from:
            doc_where.append("date(d.created_at) >= date(?)")
            doc_params.append(date_from.isoformat())
        if date_to:
            doc_where.append("date(d.created_at) <= date(?)")
            doc_params.append(date_to.isoformat())
        documents = conn.execute(
            f"""SELECT d.id, d.external_id, d.original_filename, d.document_type,
                       d.verification_status, d.extraction_status, d.created_at
                FROM documents d WHERE {' AND '.join(doc_where)}
                ORDER BY d.id DESC LIMIT ?""",
            (*doc_params, limit),
        ).fetchall()

        canonical_where = [
            """(LOWER(cr.record_type) LIKE ? ESCAPE '\\'
                OR LOWER(cr.field_name) LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM people cp
                    WHERE cp.id=cr.person_id
                      AND LOWER(cp.name) LIKE ? ESCAPE '\\'
                ))"""
        ]
        canonical_params = [pattern, pattern, pattern]
        if record_type:
            canonical_where.append("cr.record_type=?")
            canonical_params.append(record_type)
        if person_id is not None:
            canonical_where.append("cr.person_id=?")
            canonical_params.append(person_id)
        if household_id is not None:
            canonical_where.append(
                """COALESCE(
                    cr.household_id,
                    (SELECT p.household_id FROM people p WHERE p.id=cr.person_id)
                )=?"""
            )
            canonical_params.append(household_id)
        canonical_where.append(
            """(
                cr.person_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM people cp
                    WHERE cp.id=cr.person_id
                      AND LOWER(cp.name) LIKE ? ESCAPE '\\'
                )
            )"""
        )
        canonical_params.append(pattern)
        if date_from:
            canonical_where.append("date(cr.verified_at) >= date(?)")
            canonical_params.append(date_from.isoformat())
        if date_to:
            canonical_where.append("date(cr.verified_at) <= date(?)")
            canonical_params.append(date_to.isoformat())
        canonical_records = conn.execute(
            f"""SELECT cr.id, cr.person_id, cr.household_id, cr.record_type,
                       cr.field_name, cr.verified_by, cr.verified_at,
                       cr.source_document_id
                FROM canonical_records cr
                WHERE {' AND '.join(canonical_where)}
                ORDER BY cr.verified_at DESC, cr.id DESC LIMIT ?""",
            (*canonical_params, limit),
        ).fetchall()

        event_where = [
            "(LOWER(e.event_type) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(p.name, '')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(e.event_date, '')) LIKE ? ESCAPE '\\')"
        ]
        event_params = [pattern, pattern, pattern]
        if record_type == "person":
            event_where.append("1=0")
        elif record_type:
            event_where.append("e.event_type=?")
            event_params.append(record_type)
        if person_id is not None:
            event_where.append("e.person_id=?")
            event_params.append(person_id)
        if household_id is not None:
            event_where.append("COALESCE(e.household_id, p.household_id)=?")
            event_params.append(household_id)
        if date_from:
            event_where.append("date(e.event_date) >= date(?)")
            event_params.append(date_from.isoformat())
        if date_to:
            event_where.append("date(e.event_date) <= date(?)")
            event_params.append(date_to.isoformat())
        events = conn.execute(
            f"""SELECT e.id, e.person_id, e.household_id, e.event_type, e.event_date,
                       e.verification_status, e.source_document_id, e.created_at
                FROM events e LEFT JOIN people p ON p.id=e.person_id
                WHERE {' AND '.join(event_where)}
                ORDER BY e.event_date DESC, e.id DESC LIMIT ?""",
            (*event_params, limit),
        ).fetchall()

        return {
            "query": q,
            "filters": {
                "person_id": person_id,
                "household_id": household_id,
                "record_type": record_type,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
            },
            "people": [dict(row) for row in people],
            "documents": [dict(row) for row in documents],
            "canonical_records": [dict(row) for row in canonical_records],
            "events": [dict(row) for row in events],
        }


def _person_match_candidates(document_id: int):
    with connection() as conn:
        document = conn.execute(
            "SELECT id FROM documents WHERE id=?", (document_id,)
        ).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        fields = _document_fields(conn, document_id)
        evidence = {
            name: item["value"]
            for name, item in fields.items()
            if item["value"] not in (None, "")
        }
        name = evidence.get("name")
        village = evidence.get("village")
        external_id = (
            evidence.get("person_external_id")
            or evidence.get("beneficiary_id")
        )
        dob = evidence.get("date_of_birth") or evidence.get("dob")
        candidates = []
        people = conn.execute(
            """SELECT p.id, p.external_id, p.name, p.sex, p.date_of_birth,
                      p.relationship_to_head, p.household_id,
                      h.external_id AS household_external_id, h.village_id, h.address
               FROM people p JOIN households h ON h.id=p.household_id
               ORDER BY p.id"""
        ).fetchall()
        for person in people:
            identifier_match = bool(
                external_id
                and _normalize(external_id) == _normalize(person["external_id"])
            )
            if external_id and not identifier_match:
                continue
            normalized_name = _normalize(name)
            normalized_person_name = _normalize(person["name"])
            name_similarity = (
                difflib.SequenceMatcher(
                    None, normalized_name, normalized_person_name
                ).ratio()
                if normalized_name and normalized_person_name
                else 0.0
            )
            if not identifier_match and name_similarity < 0.72:
                continue
            dob_conflict = bool(
                dob
                and person["date_of_birth"]
                and _normalize(dob) != _normalize(person["date_of_birth"])
            )
            if dob_conflict:
                continue
            exact_name = bool(
                normalized_name and normalized_name == normalized_person_name
            )
            village_match = bool(
                village
                and person["village_id"]
                and _normalize(village) == _normalize(person["village_id"])
            )
            score = 1.0 if identifier_match else min(0.8 * name_similarity, 0.8)
            if village_match and not identifier_match:
                score = min(score + 0.2, 1.0)
            candidates.append(
                {
                    "person_id": person["id"],
                    "name": person["name"],
                    "sex": person["sex"],
                    "date_of_birth": person["date_of_birth"],
                    "external_id": person["external_id"],
                    "household_id": person["household_id"],
                    "household_external_id": person["household_external_id"],
                    "village_id": person["village_id"],
                    "address": person["address"],
                    "match_score": round(score, 2),
                    "matched_fields": [
                        field
                        for field, matched in (
                            ("external_id", identifier_match),
                            ("name", name_similarity >= 0.72),
                            ("village", village_match),
                            ("date_of_birth", bool(dob and person["date_of_birth"])),
                        )
                        if matched
                    ],
                    "exact_name_match": exact_name,
                    "identifier_match": identifier_match,
                }
            )
        candidates.sort(
            key=lambda candidate: (candidate["match_score"], candidate["person_id"]),
            reverse=True,
        )
        top_score = candidates[0]["match_score"] if candidates else 0
        close_matches = [
            candidate
            for candidate in candidates
            if top_score - candidate["match_score"] <= 0.05
        ]
        one_exact = (
            len(candidates) == 1
            and (candidates[0]["exact_name_match"] or candidates[0]["identifier_match"])
        )
        match_status = (
            "no_match"
            if not candidates
            else "exact"
            if one_exact
            else "ambiguous"
        )
        return {
            "document_id": document_id,
            "extracted_name": name,
            "extracted_village": village,
            "candidates": candidates,
            "match_status": match_status,
            "ambiguous": len(close_matches) > 1,
            "review_required": True,
            "auto_assign": False,
            "reason": (
                "No reliable matching evidence was found."
                if not candidates
                else "A human must confirm the candidate before linking."
            ),
        }


@router.get("/documents/{document_id}/match-candidates")
def document_match_candidates(document_id: int):
    return _person_match_candidates(document_id)


@router.get("/people/{person_id}/health-summary")
def health_summary(person_id: int):
    """Summarize stored records for one person with verification and provenance."""
    with connection() as conn:
        person = conn.execute(
            """SELECT p.*, h.village_id, h.address
               FROM people p LEFT JOIN households h ON h.id=p.household_id
               WHERE p.id=?""",
            (person_id,),
        ).fetchone()
        if not person:
            raise HTTPException(404, "Person not found")
        events = conn.execute(
            """SELECT id, event_type, event_date, details_json,
                      verification_status, verified_by, verified_at,
                      source_document_id, created_at
               FROM events WHERE person_id=?
               ORDER BY (event_date IS NULL), event_date DESC, id DESC""",
            (person_id,),
        ).fetchall()
        canonical_records = conn.execute(
            """SELECT id, record_type, field_name, value, verified_by,
                      verified_at, source_document_id, created_at, updated_at
               FROM canonical_records WHERE person_id=?
               ORDER BY verified_at DESC, id DESC""",
            (person_id,),
        ).fetchall()
        documents = conn.execute(
            """SELECT d.id, d.document_type, d.original_filename,
                      d.extraction_status, d.verification_status, d.created_at,
                      d.external_id
               FROM documents d
               JOIN document_person_links l ON l.document_id=d.id
               WHERE l.person_id=? ORDER BY d.created_at DESC, d.id DESC""",
            (person_id,),
        ).fetchall()
        by_type = {}
        for event in events:
            by_type[event["event_type"]] = by_type.get(event["event_type"], 0) + 1
        event_items = [
            {
                "id": event["id"],
                "record_type": "event",
                "event_type": event["event_type"],
                "event_date": event["event_date"],
                "details": json.loads(event["details_json"] or "{}"),
                "verification_status": event["verification_status"],
                "review_required": event["verification_status"] != "verified",
                "verified_by": event["verified_by"],
                "verified_at": event["verified_at"],
                "source_document_id": event["source_document_id"],
                "created_at": event["created_at"],
            }
            for event in events
        ]
        canonical_items = [
            {
                **dict(record),
                "record_type": record["record_type"],
                "verification_status": "verified",
                "review_required": False,
            }
            for record in canonical_records
        ]
        document_items = [
            {
                **dict(document),
                "source_document_id": document["id"],
                "review_required": (
                    document["verification_status"] != "verified"
                    or document["extraction_status"] != "extracted"
                ),
            }
            for document in documents
        ]
        return {
            "person": dict(person),
            "event_counts": by_type,
            "event_count": len(events),
            "verified_event_count": sum(
                event["verification_status"] == "verified" for event in events
            ),
            "linked_document_count": len(documents),
            "events": event_items,
            "canonical_records": canonical_items,
            "latest_events": event_items[:10],
            "linked_documents": document_items,
            "flags": {
                "has_unverified_events": any(
                    event["verification_status"] != "verified" for event in events
                ),
                "has_documents_requiring_review": any(
                    document["review_required"] for document in document_items
                ),
            },
            "disclaimer": "This is a record summary, not a diagnosis or clinical decision.",
        }
