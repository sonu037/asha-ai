import sqlite3


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


def _mark_for_review(
    conn: sqlite3.Connection,
    field_id: int,
    other_document_id: int,
    other_field_id: int,
) -> None:
    row = conn.execute(
        "SELECT review_reason FROM document_fields WHERE id=?",
        (field_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Conflict references missing field {field_id}.")
    note = (
        f"Cross-document conflict with document {other_document_id}, "
        f"field {other_field_id}; compare both source records."
    )
    reason = row["review_reason"] or ""
    if note not in reason:
        combined = f"{reason} {note}".strip()
        conn.execute(
            """
            UPDATE document_fields
            SET needs_review=1, review_reason=?
            WHERE id=?
            """,
            (combined, field_id),
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

            field_id_a = prior_field["id"]
            field_id_b = current_field["id"]
            conn.execute(
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
                SELECT id
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
            _mark_for_review(
                conn,
                prior_field["id"],
                document_id,
                current_field["id"],
            )
            _mark_for_review(
                conn,
                current_field["id"],
                prior_id,
                prior_field["id"],
            )
            conflicts.append(
                {
                    "id": conflict["id"],
                    "person_id": person_id,
                    "field_name": field_name,
                    "status": "open",
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
