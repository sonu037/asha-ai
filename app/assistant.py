import json
from fastapi import HTTPException

from .auth import Principal, require_person_access


def _normalized_question(question: str) -> str:
    return " ".join(question.casefold().split())


def _intent(question: str) -> str | None:
    if any(term in question for term in ("changed", "change between", "previous visit", "latest visit")):
        return "changes"
    if any(term in question for term in ("follow-up", "follow up", "pending task", "pending tasks")):
        return "follow_up"
    if any(term in question for term in ("missing", "incomplete")):
        return "missing"
    if any(term in question for term in ("verify", "verification", "review")):
        return "review"
    if any(term in question for term in ("document", "source", "support this summary")):
        return "documents"
    if any(term in question for term in ("event", "visit", "recorded")):
        return "events"
    return None


def _source(kind: str, source_id: int, **metadata) -> dict:
    return {"type": kind, "id": source_id, **metadata}


def _read_events(conn, person_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, event_type, event_date, details_json,
                  verification_status, source_document_id
           FROM events WHERE person_id=?
           ORDER BY (event_date IS NULL), event_date, id""",
        (person_id,),
    ).fetchall()
    return [
        {
            "event_id": row["id"],
            "event_type": row["event_type"],
            "event_date": row["event_date"],
            "verification_status": row["verification_status"],
            "details": json.loads(row["details_json"] or "{}"),
            "source_document_id": row["source_document_id"],
        }
        for row in rows
    ]


def _read_documents(conn, person_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT d.id, d.document_type, d.extraction_status,
                  d.verification_status, d.classification_confidence,
                  d.classification_needs_review, d.extraction_provider,
                  d.extraction_model, d.created_at
           FROM documents d
           JOIN document_person_links l ON l.document_id=d.id
           WHERE l.person_id=?
           ORDER BY d.created_at, d.id""",
        (person_id,),
    ).fetchall()
    return [
        {
            "document_id": row["id"],
            "document_type": row["document_type"],
            "extraction_status": row["extraction_status"],
            "verification_status": row["verification_status"],
            "classification_confidence": row["classification_confidence"],
            "classification_needs_review": bool(
                row["classification_needs_review"]
            ),
            "extraction_provider": row["extraction_provider"],
            "extraction_model": row["extraction_model"],
            "created_at": row["created_at"],
            "source": _source("document", row["id"]),
        }
        for row in rows
    ]


def _changes_between_visits(conn, person_id: int) -> dict:
    events = conn.execute(
        """SELECT id, event_date, source_document_id
           FROM events
           WHERE person_id=? AND verification_status='verified'
             AND source_document_id IS NOT NULL AND event_date IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM document_person_links l
                 WHERE l.document_id=events.source_document_id
                   AND l.person_id=events.person_id
             )
           ORDER BY event_date, id""",
        (person_id,),
    ).fetchall()
    visits = []
    for event in events:
        fields = conn.execute(
            """SELECT id, field_name, verified_value
               FROM document_fields
               WHERE document_id=? AND verified_value IS NOT NULL
                 AND needs_review=0
               ORDER BY id""",
            (event["source_document_id"],),
        ).fetchall()
        visits.append(
            {
                "event_id": event["id"],
                "event_date": event["event_date"],
                "document_id": event["source_document_id"],
                "fields": {
                    row["field_name"]: {
                        "value": row["verified_value"],
                        "field_id": row["id"],
                    }
                    for row in fields
                },
            }
        )
    if len(visits) < 2:
        return {
            "status": "unavailable",
            "reason": "Fewer than two verified, dated, source-linked visits are recorded.",
            "visits_compared": visits,
            "changes": [],
        }

    previous, latest = visits[-2:]
    common_fields = sorted(set(previous["fields"]) & set(latest["fields"]))
    changes = []
    for field_name in common_fields:
        if field_name in {"date", "event_date", "visit_date"}:
            continue
        old = previous["fields"][field_name]
        new = latest["fields"][field_name]
        if old["value"] != new["value"]:
            changes.append(
                {
                    "field_name": field_name,
                    "previous_value": old["value"],
                    "latest_value": new["value"],
                    "sources": [
                        _source(
                            "document_field",
                            old["field_id"],
                            document_id=previous["document_id"],
                            event_id=previous["event_id"],
                        ),
                        _source(
                            "document_field",
                            new["field_id"],
                            document_id=latest["document_id"],
                            event_id=latest["event_id"],
                        ),
                    ],
                }
            )
    return {
        "status": "available" if changes else "no_comparable_change",
        "visits_compared": [
            {key: visit[key] for key in ("event_id", "event_date", "document_id")}
            for visit in (previous, latest)
        ],
        "changes": changes,
        "reason": (
            None
            if changes
            else "The two latest verified visits have no differing fields in common."
        ),
    }


def answer_question(
    conn,
    principal: Principal,
    person_id: int,
    question: str,
) -> dict:
    require_person_access(conn, principal, person_id)
    person = conn.execute(
        "SELECT id FROM people WHERE id=?",
        (person_id,),
    ).fetchone()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found.")

    normalized = _normalized_question(question)
    intent = _intent(normalized)
    result: dict
    if intent == "events":
        events = _read_events(conn, person_id)
        result = {
            "status": "available" if events else "unavailable",
            "answer": events,
            "sources": [
                _source(
                    "event",
                    event["event_id"],
                    document_id=event["source_document_id"],
                )
                for event in events
            ],
            "reason": None if events else "No events are recorded for this person.",
        }
    elif intent == "documents":
        documents = _read_documents(conn, person_id)
        result = {
            "status": "available" if documents else "unavailable",
            "answer": documents,
            "sources": [item["source"] for item in documents],
            "reason": (
                None
                if documents
                else "No person-linked source documents are recorded."
            ),
        }
    elif intent == "review":
        documents = _read_documents(conn, person_id)
        review_items = [
            {
                **document,
                "reason": (
                    "Document classification requires review."
                    if document["classification_needs_review"]
                    else "Document extraction or verification is incomplete."
                ),
            }
            for document in documents
            if document["extraction_status"] != "extracted"
            or document["verification_status"] != "verified"
            or document["classification_needs_review"]
        ]
        fields = conn.execute(
            """SELECT f.id, f.document_id, f.field_name, f.extracted_value,
                      f.confidence, f.review_reason
               FROM document_fields f
               JOIN document_person_links l ON l.document_id=f.document_id
               WHERE l.person_id=?
                 AND (f.verified_value IS NULL OR f.needs_review=1)
               ORDER BY f.document_id, f.id""",
            (person_id,),
        ).fetchall()
        review_items.extend(
            {
                "field_name": row["field_name"],
                "extracted_value": row["extracted_value"],
                "confidence": row["confidence"],
                "reason": row["review_reason"] or "Field has not been verified.",
                "source": _source(
                    "document_field",
                    row["id"],
                    document_id=row["document_id"],
                ),
            }
            for row in fields
        )
        unverified_events = conn.execute(
            """SELECT id, event_type, event_date, source_document_id
               FROM events
               WHERE person_id=? AND verification_status!='verified'
               ORDER BY (event_date IS NULL), event_date, id""",
            (person_id,),
        ).fetchall()
        review_items.extend(
            {
                "event_type": row["event_type"],
                "event_date": row["event_date"],
                "reason": "Event is not verified.",
                "source": _source(
                    "event",
                    row["id"],
                    document_id=row["source_document_id"],
                ),
            }
            for row in unverified_events
        )
        result = {
            "status": "available" if review_items else "unavailable",
            "answer": review_items,
            "sources": [
                item["source"]
                for item in review_items
                if "source" in item
            ],
            "reason": None if review_items else "No records currently require review.",
        }
    elif intent == "missing":
        result = {
            "status": "unavailable",
            "answer": None,
            "sources": [],
            "reason": (
                "No required-field checklist is configured, so record completeness "
                "cannot be determined without inventing requirements."
            ),
        }
    elif intent == "follow_up":
        result = {
            "status": "unavailable",
            "answer": None,
            "sources": [],
            "reason": "This version has no follow-up task records to retrieve.",
        }
    elif intent == "changes":
        changes = _changes_between_visits(conn, person_id)
        result = {
            "status": changes["status"],
            "answer": changes["changes"],
            "sources": [
                source
                for change in changes["changes"]
                for source in change["sources"]
            ],
            "visits_compared": changes["visits_compared"],
            "reason": changes.get("reason"),
        }
    else:
        result = {
            "status": "unsupported",
            "answer": None,
            "sources": [],
            "reason": (
                "This assistant supports questions about recorded events, linked "
                "documents, review status, missingness, follow-up tasks, and "
                "changes between verified visits."
            ),
        }

    return {
        "person_id": person_id,
        "question": question,
        "intent": intent,
        "engine": "structured_database_retrieval",
        "model": None,
        **result,
        "disclaimer": (
            "This response reports stored records only; it is not a diagnosis "
            "or clinical decision."
        ),
    }


def build_longitudinal_summary(conn, person_id: int) -> dict:
    events = conn.execute(
        """SELECT id, event_type, event_date, details_json,
                  verification_status, source_document_id, created_at
           FROM events WHERE person_id=?""",
        (person_id,),
    ).fetchall()
    canonical_records = conn.execute(
        """SELECT id, field_name, value, verified_at, source_document_id
           FROM canonical_records WHERE person_id=?""",
        (person_id,),
    ).fetchall()
    documents = conn.execute(
        """SELECT d.id, d.document_type, d.extraction_status,
                  d.verification_status, d.classification_confidence,
                  d.classification_needs_review, d.extraction_provider,
                  d.extraction_model, d.created_at,
                  COALESCE(MIN(e.event_date), d.created_at) AS record_date
           FROM documents d
           JOIN document_person_links l ON l.document_id=d.id
           LEFT JOIN events e ON e.source_document_id=d.id AND e.person_id=l.person_id
           WHERE l.person_id=?
           GROUP BY d.id""",
        (person_id,),
    ).fetchall()
    records = [
        {
            "record_type": "event",
            "record_id": row["id"],
            "date": row["event_date"],
            "event_type": row["event_type"],
            "verification_status": row["verification_status"],
            "details": json.loads(row["details_json"] or "{}"),
            "source": _source(
                "event",
                row["id"],
                document_id=row["source_document_id"],
            ),
            "sort_date": row["event_date"] or row["created_at"],
        }
        for row in events
    ]
    records.extend(
        {
            "record_type": "canonical_record",
            "record_id": row["id"],
            "date": row["verified_at"],
            "field_name": row["field_name"],
            "value": row["value"],
            "verification_status": "verified",
            "source": _source(
                "canonical_record",
                row["id"],
                document_id=row["source_document_id"],
            ),
            "sort_date": row["verified_at"],
        }
        for row in canonical_records
    )
    records.extend(
        {
            "record_type": "document",
            "record_id": row["id"],
            "date": row["record_date"],
            "document_type": row["document_type"],
            "extraction_status": row["extraction_status"],
            "verification_status": row["verification_status"],
            "classification_confidence": row["classification_confidence"],
            "classification_needs_review": bool(
                row["classification_needs_review"]
            ),
            "extraction_provider": row["extraction_provider"],
            "extraction_model": row["extraction_model"],
            "source": _source("document", row["id"]),
            "sort_date": row["record_date"],
        }
        for row in documents
    )
    records.sort(
        key=lambda record: (
            record["sort_date"] is None,
            record["sort_date"] or "",
            record["record_type"],
            record["record_id"],
        )
    )
    for record in records:
        record.pop("sort_date", None)
    return {
        "chronological_records": records,
        "latest_visit_changes": _changes_between_visits(conn, person_id),
        "basis": "Stored events, canonical records, and linked documents only.",
    }
