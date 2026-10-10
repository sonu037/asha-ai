import sqlite3
import json

from fastapi import HTTPException


ACTIVE_CONFLICT_STATUSES = ("open", "needs_evidence")
RESOLVED_CONFLICT_STATUSES = (
    "resolved_source_a",
    "resolved_source_b",
    "resolved_external_evidence",
    "resolved_not_conflict",
)
CONFLICT_STATUSES = ACTIVE_CONFLICT_STATUSES + RESOLVED_CONFLICT_STATUSES


_CONTEXT_FIELDS = ("visit_date", "event_date")


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _fields_for_document(
    conn: sqlite3.Connection, document_id: int
) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM document_fields
        WHERE document_id=?
        ORDER BY id
        """,
        (document_id,),
    ).fetchall()
    fields: dict[str, sqlite3.Row] = {}
    for row in rows:
        fields.setdefault(row["field_name"], row)
    return fields


def _record_date(fields: dict[str, sqlite3.Row]) -> str | None:
    for field_name in _CONTEXT_FIELDS:
        field = fields.get(field_name)
        if field is None:
            continue
        reason = (field["review_reason"] or "").casefold()
        if "ambiguous" in reason or "invalid calendar date" in reason:
            continue
        value = _normalized(field["extracted_value"])
        if value:
            return value
    return None


def _review_history(
    conn: sqlite3.Connection,
    conflict_id: int,
    reviewer_id: str,
    previous_status: str,
    decision: str,
    reason: str,
    selected_field_id: int | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        """INSERT INTO document_conflict_reviews (
               conflict_id, reviewer_id, previous_status, decision, reason,
               selected_field_id, details_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            conflict_id,
            reviewer_id,
            previous_status,
            decision,
            reason,
            selected_field_id,
            json.dumps(details or {}, sort_keys=True),
        ),
    )
    conn.execute(
        """INSERT INTO audit_log (
               actor_id, action, entity_type, entity_id, details_json
           ) VALUES (?, ?, 'document_conflict', ?, ?)""",
        (
            reviewer_id,
            decision,
            str(conflict_id),
            json.dumps(
                {
                    "previous_status": previous_status,
                    "reason": reason,
                    "selected_field_id": selected_field_id,
                    **(details or {}),
                },
                sort_keys=True,
            ),
        ),
    )


def _invalidate_conflict_fields(
    conn: sqlite3.Connection, field_ids: tuple[int, int]
) -> dict[int, dict]:
    prior_verifications = {}
    for field_id in field_ids:
        field = conn.execute(
            """SELECT verified_value, verified_by, verified_at
               FROM document_fields WHERE id=?""",
            (field_id,),
        ).fetchone()
        if field is None:
            raise RuntimeError(f"Conflict references missing field {field_id}.")
        prior_verifications[field_id] = dict(field)
        conn.execute(
            """UPDATE document_fields
               SET needs_review=1, verified_value=NULL,
                   verified_by=NULL, verified_at=NULL,
                   review_reason=TRIM(COALESCE(review_reason, '') ||
                       ' Cross-document conflict candidate requires review.')
               WHERE id=?""",
            (field_id,),
        )
        conn.execute(
            """UPDATE documents SET verification_status='partially_verified'
               WHERE id=(SELECT document_id FROM document_fields WHERE id=?)
                 AND verification_status='verified'""",
            (field_id,),
        )
    return prior_verifications


def _same_visit(
    conn: sqlite3.Connection,
    conflict: sqlite3.Row,
    record_date: str,
    visit_value: str | None,
) -> bool:
    for document_id in (
        conflict["document_id_a"],
        conflict["document_id_b"],
    ):
        fields = _fields_for_document(conn, document_id)
        if _record_date(fields) != record_date:
            return False
        prior_visit = fields.get("anc_visit")
        if (
            prior_visit is not None
            and visit_value is not None
            and _normalized(prior_visit["extracted_value"])
            != _normalized(visit_value)
        ):
            return False
    return True


def _matches_resolved_value(
    conn: sqlite3.Connection,
    person_id: int,
    field_name: str,
    record_date: str,
    visit_value: str | None,
    current_value: str,
) -> bool:
    rows = conn.execute(
        """SELECT * FROM document_field_conflicts
           WHERE person_id=? AND field_name=?
             AND status IN (
                 'resolved_source_a', 'resolved_source_b',
                 'resolved_external_evidence'
             )
           ORDER BY id DESC""",
        (person_id, field_name),
    ).fetchall()
    for row in rows:
        if not _same_visit(conn, row, record_date, visit_value):
            continue
        selected = conn.execute(
            "SELECT extracted_value FROM document_fields WHERE id=?",
            (row["selected_field_id"],),
        ).fetchone()
        if selected and _normalized(selected["extracted_value"]) == _normalized(
            current_value
        ):
            return True
    return False


def _reopen_related_conflicts(
    conn: sqlite3.Connection,
    person_id: int,
    field_name: str,
    record_date: str,
    visit_value: str | None,
    current_value: str,
    new_conflict_id: int,
    new_document_id: int,
) -> None:
    rows = conn.execute(
        """SELECT * FROM document_field_conflicts
           WHERE person_id=? AND field_name=?
             AND status IN (
                 'resolved_source_a', 'resolved_source_b',
                 'resolved_external_evidence', 'resolved_not_conflict'
             )
           ORDER BY id""",
        (person_id, field_name),
    ).fetchall()
    for row in rows:
        if not _same_visit(conn, row, record_date, visit_value):
            continue
        if row["status"] in {
            "resolved_source_a",
            "resolved_source_b",
            "resolved_external_evidence",
        }:
            selected = conn.execute(
                "SELECT extracted_value FROM document_fields WHERE id=?",
                (row["selected_field_id"],),
            ).fetchone()
            if selected and _normalized(selected["extracted_value"]) == _normalized(
                current_value
            ):
                continue
        previous_status = row["status"]
        candidate_ids = (row["field_id_a"], row["field_id_b"])
        prior_verifications = _invalidate_conflict_fields(conn, candidate_ids)
        conn.execute(
            """UPDATE document_field_conflicts
               SET status='open', resolution_decision=NULL,
                   selected_field_id=NULL, resolution_reason=NULL,
                   resolved_by=NULL, resolved_at=NULL
               WHERE id=?""",
            (row["id"],),
        )
        _review_history(
            conn,
            row["id"],
            "system",
            previous_status,
            "conflict_reopened",
            "New contradictory evidence was linked; the prior decision needs review.",
            details={
                "new_conflict_id": new_conflict_id,
                "new_document_id": new_document_id,
                "prior_verifications": prior_verifications,
            },
        )


def assert_field_usable(
        conn: sqlite3.Connection,
        field_id: int,
        value: str | None = None,
) -> sqlite3.Row:
        """Reject active conflicts and values excluded by a resolved decision."""
        field = conn.execute(
            "SELECT * FROM document_fields WHERE id=?",
            (field_id,),
        ).fetchone()
        if field is None:
            raise HTTPException(404, "Document field not found.")
        conflicts = conn.execute(
            """SELECT * FROM document_field_conflicts
               WHERE field_id_a=? OR field_id_b=?
               ORDER BY id""",
            (field_id, field_id),
        ).fetchall()
        for conflict in conflicts:
            if conflict["status"] in ACTIVE_CONFLICT_STATUSES:
                raise HTTPException(
                    409,
                    f"Field is blocked by unresolved conflict {conflict['id']}.",
                )
            if conflict["status"] in {
                "resolved_source_a",
                "resolved_source_b",
                "resolved_external_evidence",
            }:
                if conflict["selected_field_id"] != field_id:
                    raise HTTPException(
                        409,
                        f"Field was not selected in conflict resolution {conflict['id']}.",
                    )
                if value is not None and _normalized(value) != _normalized(
                    field["extracted_value"]
                ):
                    raise HTTPException(
                        409,
                        "A conflict-selected value must match its preserved source candidate.",
                    )
        return field


def assert_person_value_usable(
        conn: sqlite3.Connection,
        person_id: int,
        field_name: str,
        value: str,
) -> None:
        conflicts = conn.execute(
            """SELECT * FROM document_field_conflicts
               WHERE person_id=? AND field_name=?
               ORDER BY id""",
            (person_id, field_name),
        ).fetchall()
        normalized_value = _normalized(value)
        for conflict in conflicts:
            if conflict["status"] in ACTIVE_CONFLICT_STATUSES:
                raise HTTPException(
                    409,
                    f"Field is blocked by unresolved conflict {conflict['id']}.",
                )
            if conflict["status"] in {
                "resolved_source_a",
                "resolved_source_b",
                "resolved_external_evidence",
            }:
                selected = conn.execute(
                    "SELECT extracted_value FROM document_fields WHERE id=?",
                    (conflict["selected_field_id"],),
                ).fetchone()
                selected_value = selected["extracted_value"] if selected else None
                if normalized_value != _normalized(selected_value):
                    raise HTTPException(
                        409,
                        f"Value is not supported by conflict resolution {conflict['id']}.",
                    )


def assert_household_value_usable(
        conn: sqlite3.Connection,
        household_id: int,
        field_name: str,
        value: str,
) -> None:
        people = conn.execute(
            "SELECT id FROM people WHERE household_id=?",
            (household_id,),
        ).fetchall()
        for person in people:
            assert_person_value_usable(conn, person["id"], field_name, value)


def assert_document_values_usable(
        conn: sqlite3.Connection,
        document_id: int,
        values: dict[str, str],
        *,
        require_verified: bool,
) -> None:
        for field_name, value in values.items():
            fields = conn.execute(
                """SELECT * FROM document_fields
                   WHERE document_id=? AND field_name=?
                   ORDER BY id""",
                (document_id, field_name),
            ).fetchall()
            for field in fields:
                assert_field_usable(conn, field["id"], value)
                if require_verified and (
                    field["verified_value"] is None
                    or field["needs_review"]
                    or _normalized(field["verified_value"]) != _normalized(value)
                ):
                    raise HTTPException(
                        409,
                        f"Event value for {field_name} must match a verified source field.",
                    )


def _resolved_rejected_field(
    conn: sqlite3.Connection, field_id: int
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM document_field_conflicts
           WHERE status IN (
               'resolved_source_a', 'resolved_source_b',
               'resolved_external_evidence'
           )
             AND selected_field_id<>?
             AND (field_id_a=? OR field_id_b=?)
           LIMIT 1""",
        (field_id, field_id, field_id),
    ).fetchone()
    return row is not None


def document_has_pending_fields(conn: sqlite3.Connection, document_id: int) -> bool:
    rows = conn.execute(
        """SELECT id, verified_value, needs_review
           FROM document_fields WHERE document_id=?""",
        (document_id,),
    ).fetchall()
    return any(
        (row["verified_value"] is None or row["needs_review"])
        and not _resolved_rejected_field(conn, row["id"])
        for row in rows
    )


def is_rejected_conflict_candidate(
    conn: sqlite3.Connection, field_id: int
) -> bool:
    return _resolved_rejected_field(conn, field_id)


def assert_no_active_document_conflicts(
    conn: sqlite3.Connection, document_id: int
) -> None:
    row = conn.execute(
        """SELECT c.id
           FROM document_field_conflicts c
           JOIN document_fields a ON a.id=c.field_id_a
           JOIN document_fields b ON b.id=c.field_id_b
           WHERE (a.document_id=? OR b.document_id=?)
             AND c.status IN ('open', 'needs_evidence')
           ORDER BY c.id
           LIMIT 1""",
        (document_id, document_id),
    ).fetchone()
    if row:
        raise HTTPException(
            409,
            f"Document has unresolved conflict {row['id']}.",
        )


def detect_linked_document_conflicts(
    conn: sqlite3.Connection,
    document_id: int,
    person_id: int,
) -> list[dict]:
    """Flag differing fields across same-person records for the same dated visit."""
    current_document = conn.execute(
        "SELECT document_type FROM documents WHERE id=?",
        (document_id,),
    ).fetchone()
    if current_document is None:
        raise RuntimeError(f"Conflict scan references missing document {document_id}.")
    document_type = current_document["document_type"]
    if not document_type or document_type == "unknown":
        return []

    current_fields = _fields_for_document(conn, document_id)
    current_date = _record_date(current_fields)
    if current_date is None:
        return []

    prior_documents = conn.execute(
        """
        SELECT d.id
        FROM document_person_links AS links
        JOIN documents AS d ON d.id=links.document_id
        WHERE links.person_id=?
          AND d.id<>?
          AND d.document_type=?
        ORDER BY d.id
        """,
        (person_id, document_id, document_type),
    ).fetchall()
    conflicts: list[dict] = []

    for prior_document in prior_documents:
        prior_id = prior_document["id"]
        prior_fields = _fields_for_document(conn, prior_id)
        if _record_date(prior_fields) != current_date:
            continue
        prior_visit = prior_fields.get("anc_visit")
        current_visit = current_fields.get("anc_visit")
        if (
            prior_visit is not None
            and current_visit is not None
            and _normalized(prior_visit["extracted_value"])
            != _normalized(current_visit["extracted_value"])
        ):
            continue

        for field_name in sorted(current_fields.keys() & prior_fields.keys()):
            if field_name in _CONTEXT_FIELDS or field_name == "anc_visit":
                continue
            current_field = current_fields[field_name]
            prior_field = prior_fields[field_name]
            current_value = current_field["extracted_value"] or ""
            prior_value = prior_field["extracted_value"] or ""
            if _normalized(current_value) == _normalized(prior_value):
                continue
            current_visit = current_fields.get("anc_visit")
            visit_value = (
                current_visit["extracted_value"]
                if current_visit is not None
                else None
            )
            if _matches_resolved_value(
                conn,
                person_id,
                field_name,
                current_date,
                visit_value,
                current_value,
            ):
                continue

            field_id_a = prior_field["id"]
            field_id_b = current_field["id"]
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO document_field_conflicts (
                    person_id,
                    field_name,
                    field_id_a,
                    document_id_a,
                    field_value_a,
                    field_id_b,
                    document_id_b,
                    field_value_b
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    field_name,
                    field_id_a,
                    prior_id,
                    prior_value,
                    field_id_b,
                    document_id,
                    current_value,
                ),
            )
            conflict = conn.execute(
                """
                SELECT *
                FROM document_field_conflicts
                WHERE field_id_a=? AND field_id_b=?
                """,
                (field_id_a, field_id_b),
            ).fetchone()
            if conflict is None:
                raise RuntimeError(
                    "Conflict row was not persisted for fields "
                    f"{field_id_a} and {field_id_b}."
                )
            if cursor.rowcount:
                prior_verifications = _invalidate_conflict_fields(
                    conn, (field_id_a, field_id_b)
                )
                _review_history(
                    conn,
                    conflict["id"],
                    "system",
                    "open",
                    "conflict_detected",
                    "Conflicting extracted candidates detected for the same visit.",
                    details={
                        "candidate_value_a": prior_value,
                        "candidate_value_b": current_value,
                        "prior_verifications": prior_verifications,
                    },
                )
            _reopen_related_conflicts(
                conn,
                person_id,
                field_name,
                current_date,
                visit_value,
                current_value,
                conflict["id"],
                document_id,
            )
            conflicts.append(
                {
                    "id": conflict["id"],
                    "person_id": person_id,
                    "field_name": field_name,
                    "status": conflict["status"],
                    "sources": [
                        {
                            "document_id": prior_id,
                            "field_id": prior_field["id"],
                            "value": prior_value,
                            "source_text": prior_field["source_text"],
                        },
                        {
                            "document_id": document_id,
                            "field_id": current_field["id"],
                            "value": current_value,
                            "source_text": current_field["source_text"],
                        },
                    ],
                }
            )
    return conflicts


def conflicts_for_document(
    conn: sqlite3.Connection, document_id: int
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            conflicts.id,
            conflicts.person_id,
            conflicts.field_name,
            conflicts.status,
            conflicts.field_id_a,
            conflicts.document_id_a,
            conflicts.field_value_a,
            field_a.source_text AS source_text_a,
            conflicts.field_id_b,
            conflicts.document_id_b,
            conflicts.field_value_b,
            field_b.source_text AS source_text_b
        FROM document_field_conflicts AS conflicts
        JOIN document_fields AS field_a ON field_a.id=conflicts.field_id_a
        JOIN document_fields AS field_b ON field_b.id=conflicts.field_id_b
        WHERE conflicts.document_id_a=? OR conflicts.document_id_b=?
        ORDER BY conflicts.id
        """,
        (document_id, document_id),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "person_id": row["person_id"],
            "field_name": row["field_name"],
            "status": row["status"],
            "sources": [
                {
                    "document_id": row["document_id_a"],
                    "field_id": row["field_id_a"],
                    "value": row["field_value_a"],
                    "source_text": row["source_text_a"],
                },
                {
                    "document_id": row["document_id_b"],
                    "field_id": row["field_id_b"],
                    "value": row["field_value_b"],
                    "source_text": row["source_text_b"],
                },
            ],
        }
        for row in rows
    ]


def conflict_details(conn: sqlite3.Connection, conflict_id: int) -> dict:
    row = conn.execute(
        """SELECT c.*, p.name AS person_name,
                  d_a.document_type AS document_type_a,
                  d_a.original_filename AS filename_a,
                  d_b.document_type AS document_type_b,
                  d_b.original_filename AS filename_b,
                  a.source_text AS source_text_a,
                  a.verified_value AS verified_value_a,
                  a.needs_review AS needs_review_a,
                  b.source_text AS source_text_b,
                  b.verified_value AS verified_value_b,
                  b.needs_review AS needs_review_b
           FROM document_field_conflicts c
           JOIN people p ON p.id=c.person_id
           JOIN documents d_a ON d_a.id=c.document_id_a
           JOIN documents d_b ON d_b.id=c.document_id_b
           JOIN document_fields a ON a.id=c.field_id_a
           JOIN document_fields b ON b.id=c.field_id_b
           WHERE c.id=?""",
        (conflict_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Conflict not found.")
    history_rows = conn.execute(
        """SELECT id, reviewer_id, previous_status, decision, reason,
                  selected_field_id, details_json, created_at
           FROM document_conflict_reviews
           WHERE conflict_id=? ORDER BY id""",
        (conflict_id,),
    ).fetchall()
    context_fields = _fields_for_document(conn, row["document_id_a"])
    record_date = _record_date(context_fields)
    visit = context_fields.get("anc_visit")
    visit_value = visit["extracted_value"] if visit is not None else None
    candidates = conn.execute(
        """SELECT f.*, d.document_type, d.original_filename, d.id AS doc_id
           FROM document_fields f
           JOIN documents d ON d.id=f.document_id
           JOIN document_person_links l ON l.document_id=d.id
           WHERE l.person_id=? AND f.field_name=?
           ORDER BY d.id, f.id""",
        (row["person_id"], row["field_name"]),
    ).fetchall()
    candidate_sources = []
    for candidate in candidates:
        candidate_context = _fields_for_document(conn, candidate["doc_id"])
        candidate_visit = candidate_context.get("anc_visit")
        candidate_visit_value = (
            candidate_visit["extracted_value"]
            if candidate_visit is not None
            else None
        )
        if (
            record_date is not None
            and _record_date(candidate_context) != record_date
        ):
            continue
        if (
            visit_value is not None
            and candidate_visit_value is not None
            and _normalized(visit_value) != _normalized(candidate_visit_value)
        ):
            continue
        candidate_sources.append(
            {
                "document_id": candidate["doc_id"],
                "document_type": candidate["document_type"],
                "filename": candidate["original_filename"],
                "field_id": candidate["id"],
                "value": candidate["extracted_value"],
                "source_text": candidate["source_text"],
                "verified_value": candidate["verified_value"],
                "needs_review": bool(candidate["needs_review"]),
            }
        )
    return {
        "id": row["id"],
        "person_id": row["person_id"],
        "person_name": row["person_name"],
        "field_name": row["field_name"],
        "status": row["status"],
        "resolution_decision": row["resolution_decision"],
        "selected_field_id": row["selected_field_id"],
        "resolution_reason": row["resolution_reason"],
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
        "sources": [
            {
                "document_id": row["document_id_a"],
                "document_type": row["document_type_a"],
                "filename": row["filename_a"],
                "field_id": row["field_id_a"],
                "value": row["field_value_a"],
                "source_text": row["source_text_a"],
                "verified_value": row["verified_value_a"],
                "needs_review": bool(row["needs_review_a"]),
            },
            {
                "document_id": row["document_id_b"],
                "document_type": row["document_type_b"],
                "filename": row["filename_b"],
                "field_id": row["field_id_b"],
                "value": row["field_value_b"],
                "source_text": row["source_text_b"],
                "verified_value": row["verified_value_b"],
                "needs_review": bool(row["needs_review_b"]),
            },
        ],
        "candidate_sources": candidate_sources,
        "history": [
            {
                **dict(item),
                "details": json.loads(item["details_json"] or "{}"),
            }
            for item in history_rows
        ],
    }


def adjudicate_conflict(
    conn: sqlite3.Connection,
    conflict_id: int,
    reviewer_id: str,
    decision: str,
    reason: str,
    evidence_field_id: int | None = None,
) -> dict:
    conflict = conn.execute(
        "SELECT * FROM document_field_conflicts WHERE id=?",
        (conflict_id,),
    ).fetchone()
    if conflict is None:
        raise HTTPException(404, "Conflict not found.")
    if conflict["status"] not in ACTIVE_CONFLICT_STATUSES:
        raise HTTPException(
            409,
            "Only open conflicts or conflicts awaiting evidence can be reviewed.",
        )
    selected_field_id = None
    if decision == "resolve_source_a":
        selected_field_id = conflict["field_id_a"]
    elif decision == "resolve_source_b":
        selected_field_id = conflict["field_id_b"]
    elif decision == "resolve_with_evidence":
        selected_field_id = evidence_field_id
        if selected_field_id is None:
            raise HTTPException(
                422,
                "evidence_field_id is required when resolving with further evidence.",
            )
    elif decision not in {"request_evidence", "not_conflict"}:
        raise HTTPException(422, "Unsupported conflict decision.")
    elif evidence_field_id is not None:
        raise HTTPException(
            422,
            "evidence_field_id is only valid when resolving with further evidence.",
        )

    base_fields = _fields_for_document(conn, conflict["document_id_a"])
    base_date = _record_date(base_fields)
    base_visit_field = base_fields.get("anc_visit")
    base_visit_value = (
        base_visit_field["extracted_value"]
        if base_visit_field is not None
        else None
    )
    if selected_field_id is not None:
        candidate = conn.execute(
            """SELECT f.id, f.field_name, f.extracted_value, d.id AS document_id
               FROM document_fields f
               JOIN documents d ON d.id=f.document_id
               JOIN document_person_links l ON l.document_id=d.id
               WHERE f.id=? AND l.person_id=?""",
            (selected_field_id, conflict["person_id"]),
        ).fetchone()
        evidence_fields = (
            _fields_for_document(conn, candidate["document_id"])
            if candidate
            else {}
        )
        evidence_visit = evidence_fields.get("anc_visit")
        if (
            candidate is None
            or candidate["field_name"] != conflict["field_name"]
            or not candidate["extracted_value"]
            or base_date is None
            or _record_date(evidence_fields) != base_date
            or (
                base_visit_value is not None
                and evidence_visit is not None
                and _normalized(base_visit_value)
                != _normalized(evidence_visit["extracted_value"])
            )
        ):
            raise HTTPException(
                422,
                "Selected evidence must be linked to the same person and match the conflict field and visit.",
            )
        if decision in {"resolve_source_a", "resolve_source_b"} and (
            evidence_field_id is not None
        ):
            raise HTTPException(
                422,
                "evidence_field_id is only valid when resolving with further evidence.",
            )

    rows = conn.execute(
        """SELECT * FROM document_field_conflicts
           WHERE person_id=? AND field_name=?
             AND status IN ('open', 'needs_evidence')
           ORDER BY id""",
        (conflict["person_id"], conflict["field_name"]),
    ).fetchall()
    affected = [
        row
        for row in rows
        if base_date is not None
        and _same_visit(conn, row, base_date, base_visit_value)
    ]
    if not affected:
        affected = [conflict]

    details = {
        "candidate_value_a": conflict["field_value_a"],
        "candidate_value_b": conflict["field_value_b"],
    }
    if selected_field_id is not None:
        selected = conn.execute(
            "SELECT extracted_value FROM document_fields WHERE id=?",
            (selected_field_id,),
        ).fetchone()
        details["selected_value"] = selected["extracted_value"]
        details["evidence_field_id"] = selected_field_id

    for affected_conflict in affected:
        if decision == "request_evidence":
            affected_status = "needs_evidence"
        elif decision == "not_conflict":
            affected_status = "resolved_not_conflict"
        elif selected_field_id == affected_conflict["field_id_a"]:
            affected_status = "resolved_source_a"
        elif selected_field_id == affected_conflict["field_id_b"]:
            affected_status = "resolved_source_b"
        else:
            affected_status = "resolved_external_evidence"

        if selected_field_id is not None:
            for losing_field_id in (
                affected_conflict["field_id_a"],
                affected_conflict["field_id_b"],
            ):
                if losing_field_id == selected_field_id:
                    continue
                conn.execute(
                    """UPDATE document_fields
                       SET needs_review=0,
                           review_reason=TRIM(COALESCE(review_reason, '') ||
                               ' Conflict ' || ? || ' adjudicated in favor of '
                               || 'another source; original extracted value '
                               || 'retained as rejected candidate.')
                       WHERE id=?""",
                    (str(affected_conflict["id"]), losing_field_id),
                )
                conn.execute(
                    """UPDATE document_fields
                       SET verified_value=NULL, verified_by=NULL, verified_at=NULL
                       WHERE id=?""",
                    (losing_field_id,),
                )
        conn.execute(
            """UPDATE document_field_conflicts
               SET status=?, resolution_decision=?, selected_field_id=?,
                       resolution_reason=?, resolved_by=CASE WHEN ? IN (
                           'resolved_source_a', 'resolved_source_b',
                           'resolved_external_evidence', 'resolved_not_conflict'
                       ) THEN ? ELSE NULL END, resolved_at=
                       CASE WHEN ? IN (
                           'resolved_source_a', 'resolved_source_b',
                           'resolved_external_evidence', 'resolved_not_conflict'
                       ) THEN CURRENT_TIMESTAMP ELSE NULL END
               WHERE id=?""",
            (
                affected_status,
                decision,
                selected_field_id,
                reason,
                affected_status,
                reviewer_id,
                affected_status,
                affected_conflict["id"],
            ),
        )
        _review_history(
            conn,
            affected_conflict["id"],
            reviewer_id,
            affected_conflict["status"],
            decision,
            reason,
            selected_field_id,
            {
                **details,
                "reviewed_conflict_id": conflict_id,
                "candidate_value_a": affected_conflict["field_value_a"],
                "candidate_value_b": affected_conflict["field_value_b"],
            },
        )
    return conflict_details(conn, conflict_id)
