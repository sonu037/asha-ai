import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Request, UploadFile, HTTPException

from .config import DOCUMENT_DIR
from .db import connection, init_db
from .auth import actor_for_request, authorize_request, require_document_access
from .ai import get_provider
from .models import (
    HouseholdCreate,
    PersonCreate,
    EventCreate,
    FieldVerification,
    ClassificationVerification,
    CanonicalRecordCreate,
    DocumentPersonLink,
    DocumentEventCreate,
)
from .batch_a import (
    _person_match_candidates,
    auto_create_event_from_verified_document,
    router as batch_a_router,
)
from .classification import DOCUMENT_TYPES
from .assistant_routes import router as assistant_router
from .document_intelligence import tesseract_provider
from .document_conflicts import (
    conflicts_for_document,
    detect_linked_document_conflicts,
)
from .extraction import date_validation_reason


app = FastAPI(
    title="ASHA AI",
    version="0.2.0",
    description="AI-first ASHA documentation and record intelligence platform.",
    dependencies=[Depends(authorize_request)],
)

app.include_router(batch_a_router)
app.include_router(assistant_router)


@app.on_event("startup")
def startup():
    init_db()
    get_provider()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "product": "ASHA AI",
        "version": "0.2.0",
    }


@app.post("/households")
def create_household(payload: HouseholdCreate, request: Request):
    with connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO households (
                    external_id,
                    village_id,
                    address
                )
                VALUES (?, ?, ?)
                """,
                (
                    payload.external_id,
                    payload.village_id,
                    payload.address,
                ),
            )
        except Exception as e:
            raise HTTPException(
                409,
                f"Household could not be created: {e}",
            )
        conn.execute(
            """INSERT INTO audit_log (
                   actor_id, action, entity_type, entity_id, details_json
               ) VALUES (?, 'create_household', 'household', ?, '{}')""",
            (actor_for_request(request), str(cur.lastrowid)),
        )

        return {
            "id": cur.lastrowid,
            **payload.model_dump(),
        }


@app.post("/people")
def create_person(payload: PersonCreate, request: Request):
    with connection() as conn:
        if not conn.execute(
            "SELECT id FROM households WHERE id=?",
            (payload.household_id,),
        ).fetchone():
            raise HTTPException(
                404,
                "Household not found",
            )

        cur = conn.execute(
            """
            INSERT INTO people (
                external_id,
                household_id,
                name,
                sex,
                date_of_birth,
                relationship_to_head
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            tuple(payload.model_dump().values()),
        )
        conn.execute(
            """INSERT INTO audit_log (
                   actor_id, action, entity_type, entity_id, details_json
               ) VALUES (?, 'create_person', 'person', ?, '{}')""",
            (actor_for_request(request), str(cur.lastrowid)),
        )

        return {
            "id": cur.lastrowid,
            **payload.model_dump(),
        }


@app.get("/households")
def list_households(request: Request):
    with connection() as conn:
        principal = request.state.principal
        if principal.is_admin:
            rows = conn.execute(
                "SELECT * FROM households ORDER BY id DESC"
            ).fetchall()
        else:
            person_ids = sorted(principal.person_ids)
            household_ids = sorted(principal.household_ids)
            predicates = []
            params = []
            if person_ids:
                predicates.append(
                    f"id IN (SELECT household_id FROM people WHERE id IN ({','.join('?' for _ in person_ids)}))"
                )
                params.extend(person_ids)
            if household_ids:
                predicates.append(
                    f"id IN ({','.join('?' for _ in household_ids)})"
                )
                params.extend(household_ids)
            if predicates:
                rows = conn.execute(
                    f"SELECT * FROM households WHERE {' OR '.join(predicates)} ORDER BY id DESC",
                    params,
                ).fetchall()
            else:
                rows = []
        return [
            dict(row)
            for row in rows
        ]


@app.get("/households/{household_id}/timeline")
def household_timeline(household_id: int):
    with connection() as conn:
        if not conn.execute(
            "SELECT id FROM households WHERE id=?",
            (household_id,),
        ).fetchone():
            raise HTTPException(
                404,
                "Household not found",
            )

        rows = conn.execute(
            """
            SELECT *
            FROM events
            WHERE household_id=?
            ORDER BY event_date DESC, id DESC
            """,
            (household_id,),
        ).fetchall()

        return [
            {
                "event_type": row["event_type"],
                "event_date": row["event_date"],
                "details": json.loads(
                    row["details_json"] or "{}"
                ),
                "verified": row["verification_status"] == "verified",
                "verified_by": row["verified_by"],
                "verified_at": row["verified_at"],
                "source_document_id": row["source_document_id"],
            }
            for row in rows
        ]


@app.get("/people/{person_id}")
def get_person(person_id: int):
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE id=?",
            (person_id,),
        ).fetchone()

        if not row:
            raise HTTPException(
                404,
                "Person not found",
            )

        return dict(row)


@app.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(
            415,
            "Only JPEG, PNG, and WebP image documents are supported by the current OCR pipeline.",
        )
    content_digest = hashlib.sha256()
    content_length = 0
    while chunk := await file.read(1024 * 1024):
        content_length += len(chunk)
        if content_length > 10 * 1024 * 1024:
            raise HTTPException(413, "Document uploads are limited to 10 MiB.")
        content_digest.update(chunk)
    content_hash = content_digest.hexdigest()
    with connection() as conn:
        existing = conn.execute(
            "SELECT id, external_id FROM documents WHERE content_hash=?",
            (content_hash,),
        ).fetchone()
    if existing:
        try:
            with connection() as conn:
                require_document_access(
                    conn,
                    request.state.principal,
                    existing["id"],
                )
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise HTTPException(
                409,
                "Identical content is already stored but is outside your access scope.",
            ) from None
        return {
            "status": "duplicate",
            "document_id": existing["id"],
            "external_id": existing["external_id"],
            "duplicate_of": existing["id"],
        }

    doc_id = str(uuid.uuid4())
    target = DOCUMENT_DIR / f"{doc_id}{suffix}"
    await file.seek(0)
    try:
        with target.open("wb") as stored_file:
            while chunk := await file.read(1024 * 1024):
                stored_file.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    external_id = (
        f"ASHA-DOC-{doc_id[:8].upper()}"
    )

    try:
        with connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO documents (
                    external_id,
                    original_filename,
                    storage_path,
                    content_hash,
                    created_by
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    external_id,
                    file.filename,
                    str(
                        target.relative_to(
                            DOCUMENT_DIR.parent
                        )
                    ),
                    content_hash,
                    request.state.principal.subject,
                ),
            )
            numeric_id = cur.lastrowid
            conn.execute(
                """INSERT INTO audit_log (
                       actor_id, action, entity_type, entity_id, details_json
                   ) VALUES (?, 'upload_document', 'document', ?, '{}')""",
                (request.state.principal.subject, str(numeric_id)),
            )
    except sqlite3.IntegrityError:
        target.unlink(missing_ok=True)
        with connection() as conn:
            existing = conn.execute(
                "SELECT id, external_id FROM documents WHERE content_hash=?",
                (content_hash,),
            ).fetchone()
        if not existing:
            raise
        try:
            with connection() as conn:
                require_document_access(
                    conn,
                    request.state.principal,
                    existing["id"],
                )
        except HTTPException as access_error:
            if access_error.status_code != 404:
                raise
            raise HTTPException(
                409,
                "Identical content is already stored but is outside your access scope.",
            ) from None
        return {
            "status": "duplicate",
            "document_id": existing["id"],
            "external_id": existing["external_id"],
            "duplicate_of": existing["id"],
        }
    except Exception:
        target.unlink(missing_ok=True)
        raise

    result = get_provider().extract(str(target))

    with connection() as conn:
        conn.execute(
            """
            UPDATE documents
            SET
                document_type=?,
                language=?,
                ocr_text=?,
                extraction_status=?,
                extraction_provider=?,
                extraction_model=?,
                processing_version=?,
                extraction_warnings=?,
                classification_confidence=?,
                classification_needs_review=?
            WHERE id=?
            """,
            (
                result.document_type,
                result.language,
                result.raw_text,
                result.extraction_status,
                result.provider,
                result.model,
                result.processing_version,
                json.dumps(result.warnings),
                result.classification_confidence,
                int(result.classification_needs_review),
                numeric_id,
            ),
        )

        for fact in result.facts:
            conn.execute(
                """
                INSERT INTO document_fields (
                    document_id,
                    field_name,
                    extracted_value,
                    confidence,
                    page_number,
                    bounding_box,
                    extraction_method,
                    needs_review,
                    review_reason,
                    source_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    numeric_id,
                    fact.field_name,
                    str(fact.value),
                    fact.confidence,
                    fact.page_number,
                    (
                        json.dumps(fact.bounding_box)
                        if fact.bounding_box
                        else None
                    ),
                    fact.method,
                    int(fact.needs_review),
                    fact.reason,
                    fact.source_text,
                ),
            )

    return {
        "status": "created",
        "document_id": numeric_id,
        "external_id": external_id,
        "filename": file.filename,
        "extraction_status": result.extraction_status,
        "extraction": result.model_dump(),
    }


@app.get("/documents/{document_id}")
def get_document(document_id: int):
    with connection() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()

        if not doc:
            raise HTTPException(
                404,
                "Document not found",
            )

        fields = conn.execute(
            """
            SELECT *
            FROM document_fields
            WHERE document_id=?
            ORDER BY id
            """,
            (document_id,),
        ).fetchall()

        return {
            "document": dict(doc),
            "fields": [dict(field) for field in fields],
            "conflicts": conflicts_for_document(conn, document_id),
        }


@app.post("/documents/{document_id}/classification/verify")
def verify_document_classification(
    document_id: int,
    payload: ClassificationVerification,
    request: Request,
):
    if payload.document_type not in DOCUMENT_TYPES:
        raise HTTPException(422, "Unsupported document type.")
    actor_id = actor_for_request(request, payload.verified_by)
    automatic_event = {"status": "not_ready"}
    with connection() as conn:
        document = conn.execute(
            "SELECT id, verification_status FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if not document:
            raise HTTPException(404, "Document not found.")
        conn.execute(
            """UPDATE documents
               SET document_type=?, classification_needs_review=0
               WHERE id=?""",
            (payload.document_type, document_id),
        )
        conn.execute(
            """INSERT INTO audit_log (
                   actor_id, action, entity_type, entity_id, details_json
               ) VALUES (?, 'verify_document_classification', 'document', ?, ?)""",
            (
                actor_id,
                str(document_id),
                json.dumps({"document_type": payload.document_type}),
            ),
        )
        if document["verification_status"] == "verified":
            automatic_event = auto_create_event_from_verified_document(
                conn,
                document_id,
                actor_id,
            )
    return {
        "status": "verified",
        "document_id": document_id,
        "document_type": payload.document_type,
        "verified_by": actor_id,
        "automatic_event": automatic_event,
    }


@app.get("/documents/{document_id}/person-candidates")
def document_person_candidates(document_id: int, request: Request):
    return _person_match_candidates(document_id, request.state.principal)


@app.post("/documents/{document_id}/link-person")
def link_document_to_person(
    document_id: int,
    payload: DocumentPersonLink,
    request: Request,
):
    actor_id = actor_for_request(request, payload.linked_by)
    with connection() as conn:
        document = conn.execute(
            """
            SELECT id
            FROM documents
            WHERE id=?
            """,
            (document_id,),
        ).fetchone()

        if not document:
            raise HTTPException(
                404,
                "Document not found",
            )

        person = conn.execute(
            """
            SELECT id
            FROM people
            WHERE id=?
            """,
            (payload.person_id,),
        ).fetchone()

        if not person:
            raise HTTPException(
                404,
                "Person not found",
            )

        existing = conn.execute(
            """
            SELECT *
            FROM document_person_links
            WHERE document_id=?
            """,
            (document_id,),
        ).fetchone()

        if existing:
            raise HTTPException(
                409,
                "Document is already linked to a person",
            )

        conn.execute(
            """
            INSERT INTO document_person_links (
                document_id,
                person_id,
                linked_by
            )
            VALUES (?, ?, ?)
            """,
            (
                document_id,
                payload.person_id,
                actor_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO audit_log (
                actor_id,
                action,
                entity_type,
                entity_id,
                details_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                "link_document_to_person",
                "document",
                str(document_id),
                json.dumps(
                    {
                        "person_id": payload.person_id,
                    }
                ),
            ),
        )
        conflicts = detect_linked_document_conflicts(
            conn,
            document_id,
            payload.person_id,
        )

        return {
            "status": "linked",
            "document_id": document_id,
            "person_id": payload.person_id,
            "linked_by": actor_id,
            "conflicts": conflicts,
        }
@app.post("/documents/{document_id}/create-event")
def create_event_from_document(
    document_id: int,
    payload: DocumentEventCreate,
    request: Request,
):
    actor_id = actor_for_request(request, payload.verified_by)
    with connection() as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id=?
            """,
            (document_id,),
        ).fetchone()

        if not document:
            raise HTTPException(
                404,
                "Document not found",
            )

        if document["verification_status"] != "verified":
            raise HTTPException(
                409,
                "Document must be fully verified before creating an event",
            )
        if document["classification_needs_review"]:
            raise HTTPException(
                409,
                "Document classification must be verified before creating an event.",
            )

        person = conn.execute(
            """
            SELECT *
            FROM people
            WHERE id=?
            """,
            (payload.person_id,),
        ).fetchone()

        if not person:
            raise HTTPException(
                404,
                "Person not found",
            )

        link = conn.execute(
            """
            SELECT *
            FROM document_person_links
            WHERE document_id=?
              AND person_id=?
            """,
            (
                document_id,
                payload.person_id,
            ),
        ).fetchone()

        if not link:
            raise HTTPException(
                409,
                "Document is not linked to this person",
            )

        details_json = json.dumps(payload.details, sort_keys=True, separators=(",", ":"))
        duplicate = conn.execute(
            """SELECT id FROM events
               WHERE source_document_id=? AND person_id=? AND event_type=?
                 AND COALESCE(event_date, '')=COALESCE(?, '')
                 AND COALESCE(details_json, '')=?""",
            (
                document_id,
                payload.person_id,
                payload.event_type,
                payload.event_date,
                details_json,
            ),
        ).fetchone()
        if duplicate:
            return {
                "status": "duplicate",
                "event_id": duplicate["id"],
                "document_id": document_id,
                "person_id": payload.person_id,
            }

        external_id = (
            f"DOC-EVENT-{document_id}-{uuid.uuid4().hex[:8].upper()}"
        )

        cur = conn.execute(
            """
            INSERT INTO events (
                external_id,
                household_id,
                person_id,
                asha_id,
                event_type,
                event_date,
                details_json,
                source_document_id,
                verification_status,
                verified_by,
                verified_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?,
                CURRENT_TIMESTAMP
            )
            """,
            (
                external_id,
                None,
                payload.person_id,
                None,
                payload.event_type,
                payload.event_date,
                details_json,
                document_id,
                actor_id,
            ),
        )

        event_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO audit_log (
                actor_id,
                action,
                entity_type,
                entity_id,
                details_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                "create_event_from_document",
                "event",
                str(event_id),
                json.dumps(
                    {
                        "document_id": document_id,
                        "person_id": payload.person_id,
                    }
                ),
            ),
        )

        return {
            "status": "created",
            "event_id": event_id,
            "document_id": document_id,
            "person_id": payload.person_id,
            "event_type": payload.event_type,
            "event_date": payload.event_date,
            "verification_status": "verified",
            "verified_by": actor_id,
        }
@app.post(
    "/documents/{document_id}/fields/{field_id}/verify"
)
def verify_field(
    document_id: int,
    field_id: int,
    payload: FieldVerification,
    request: Request,
):
    actor_id = actor_for_request(request, payload.verified_by)
    automatic_event = {"status": "not_ready"}
    with connection() as conn:
        field = conn.execute(
            """
            SELECT field_name
            FROM document_fields
            WHERE id=? AND document_id=?
            """,
            (field_id, document_id),
        ).fetchone()
        if field is None:
            raise HTTPException(404, "Field not found")
        if field["field_name"] in {"visit_date", "lmp", "edd"}:
            validation_reason = date_validation_reason(payload.verified_value)
            if validation_reason is not None:
                raise HTTPException(
                    422,
                    "A date cannot be verified in an invalid or ambiguous format. "
                    "Enter a valid, unambiguous date such as YYYY-MM-DD.",
                )
        cur = conn.execute(
            """
            UPDATE document_fields
            SET
                verified_value=?,
                verified_by=?,
                verified_at=CURRENT_TIMESTAMP,
                needs_review=0
            WHERE id=?
              AND document_id=?
            """,
            (
                payload.verified_value,
                actor_id,
                field_id,
                document_id,
            ),
        )

        if cur.rowcount == 0:
            raise HTTPException(
                404,
                "Field not found",
            )

        remaining = conn.execute(
            """
            SELECT COUNT(*) n
            FROM document_fields
            WHERE document_id=?
              AND (verified_value IS NULL OR needs_review=1)
            """,
            (document_id,),
        ).fetchone()["n"]

        status = (
            "verified"
            if remaining == 0
            else "partially_verified"
        )

        conn.execute(
            """
            UPDATE documents
            SET verification_status=?
            WHERE id=?
            """,
            (
                status,
                document_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO audit_log (
                actor_id,
                action,
                entity_type,
                entity_id,
                details_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                "verify_field",
                "document_field",
                str(field_id),
                json.dumps(
                    {
                        "document_id": document_id,
                    }
                ),
            ),
        )
        if status == "verified":
            automatic_event = auto_create_event_from_verified_document(
                conn,
                document_id,
                actor_id,
            )

    return {
        "status": "verified",
        "field_id": field_id,
        "document_status": status,
        "automatic_event": automatic_event,
    }


@app.post("/events")
def create_event(payload: EventCreate, request: Request):
    principal = request.state.principal
    if not principal.is_admin and payload.verification_status == "verified":
        raise HTTPException(
            403,
            "Only an administrator may create an already-verified event.",
        )
    actor_id = actor_for_request(request, payload.verified_by or payload.asha_id)
    event_status = payload.verification_status if principal.is_admin else "unverified"
    verified_by = actor_id if event_status == "verified" else None
    with connection() as conn:

        # Verify referenced person if provided
        if payload.person_id is not None:
            person = conn.execute(
                "SELECT id, household_id FROM people WHERE id=?",
                (payload.person_id,),
            ).fetchone()
            if not person:
                raise HTTPException(
                    404,
                    "Person not found",
                )
            if (
                payload.household_id is not None
                and payload.household_id != person["household_id"]
            ):
                raise HTTPException(
                    409,
                    "Person does not belong to the specified household.",
                )

        # Verify referenced household if provided
        if payload.household_id is not None:
            if not conn.execute(
                "SELECT id FROM households WHERE id=?",
                (payload.household_id,),
            ).fetchone():
                raise HTTPException(
                    404,
                    "Household not found",
                )

        # Verify source document if provided
        if payload.source_document_id is not None:
            source_document = conn.execute(
                "SELECT id, classification_needs_review FROM documents WHERE id=?",
                (payload.source_document_id,),
            ).fetchone()
            if not source_document:
                raise HTTPException(
                    404,
                    "Source document not found",
                )
            if (
                event_status == "verified"
                and source_document["classification_needs_review"]
            ):
                raise HTTPException(
                    409,
                    "Document classification must be verified before creating a verified event.",
                )
            if not principal.is_admin:
                if payload.person_id is not None:
                    linked = conn.execute(
                        """SELECT 1 FROM document_person_links
                           WHERE document_id=? AND person_id=?""",
                        (payload.source_document_id, payload.person_id),
                    ).fetchone()
                else:
                    linked = conn.execute(
                        """SELECT 1 FROM document_person_links l
                           JOIN people p ON p.id=l.person_id
                           WHERE l.document_id=? AND p.household_id=?""",
                        (payload.source_document_id, payload.household_id),
                    ).fetchone()
                if not linked:
                    raise HTTPException(
                        409,
                        "Source document must be linked to the event person or household.",
                    )

        cur = conn.execute(
            """
            INSERT INTO events (
                external_id,
                household_id,
                person_id,
                asha_id,
                event_type,
                event_date,
                details_json,
                source_document_id,
                verification_status,
                verified_by,
                verified_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CASE
                        WHEN ? IS NOT NULL
                        THEN CURRENT_TIMESTAMP
                        ELSE NULL
                    END)
            """,
            (
                payload.external_id,
                payload.household_id,
                payload.person_id,
                payload.asha_id if principal.is_admin else actor_id,
                payload.event_type,
                payload.event_date,
                json.dumps(payload.details),
                payload.source_document_id,
                event_status,
                verified_by,
                verified_by,
            ),
        )

        event_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO audit_log (
                actor_id,
                action,
                entity_type,
                entity_id,
                details_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                "create_event",
                "event",
                str(event_id),
                json.dumps(
                    {
                        "event_type": payload.event_type,
                        "verification_status": (
                            event_status
                        ),
                        "source_document_id": (
                            payload.source_document_id
                        ),
                    }
                ),
            ),
        )

        return {
            "id": event_id,
            **payload.model_copy(
                update={
                    "asha_id": payload.asha_id if principal.is_admin else actor_id,
                    "verification_status": event_status,
                    "verified_by": verified_by,
                    "verified_at": None,
                }
            ).model_dump(),
        }


@app.post("/canonical-records")
def create_canonical_record(
    payload: CanonicalRecordCreate,
    request: Request,
):
    actor_id = actor_for_request(request, payload.verified_by)
    with connection() as conn:

        # Verify referenced person if provided
        if payload.person_id is not None:
            person = conn.execute(
                "SELECT id, household_id FROM people WHERE id=?",
                (payload.person_id,),
            ).fetchone()
            if not person:
                raise HTTPException(
                    404,
                    "Person not found",
                )
            if (
                payload.household_id is not None
                and payload.household_id != person["household_id"]
            ):
                raise HTTPException(
                    409,
                    "Person does not belong to the specified household.",
                )

        # Verify referenced household if provided
        if payload.household_id is not None:
            if not conn.execute(
                "SELECT id FROM households WHERE id=?",
                (payload.household_id,),
            ).fetchone():
                raise HTTPException(
                    404,
                    "Household not found",
                )

        # Verify source document if provided
        if payload.source_document_id is not None:
            source_document = conn.execute(
                "SELECT id FROM documents WHERE id=?",
                (payload.source_document_id,),
            ).fetchone()
            if not source_document:
                raise HTTPException(
                    404,
                    "Source document not found",
                )
            if not request.state.principal.is_admin:
                if payload.person_id is not None:
                    linked = conn.execute(
                        """SELECT 1 FROM document_person_links
                           WHERE document_id=? AND person_id=?""",
                        (payload.source_document_id, payload.person_id),
                    ).fetchone()
                elif payload.household_id is not None:
                    linked = conn.execute(
                        """SELECT 1 FROM document_person_links l
                           JOIN people p ON p.id=l.person_id
                           WHERE l.document_id=? AND p.household_id=?""",
                        (payload.source_document_id, payload.household_id),
                    ).fetchone()
                else:
                    linked = None
                if not linked:
                    raise HTTPException(
                        409,
                        "Source document must be linked to the canonical record person or household.",
                    )
            verified_field = conn.execute(
                """SELECT id FROM document_fields
                   WHERE document_id=? AND field_name=? AND verified_value=?
                     AND needs_review=0""",
                (
                    payload.source_document_id,
                    payload.field_name,
                    payload.value,
                ),
            ).fetchone()
            if not verified_field:
                raise HTTPException(
                    409,
                    "Canonical values must match a verified source-document field",
                )

        existing = conn.execute(
            """SELECT * FROM canonical_records
               WHERE person_id IS ? AND household_id IS ?
                 AND record_type=? AND field_name=? AND value=?
                 AND source_document_id IS ?""",
            (
                payload.person_id,
                payload.household_id,
                payload.record_type,
                payload.field_name,
                payload.value,
                payload.source_document_id,
            ),
        ).fetchone()
        if existing:
            return {
                "id": existing["id"],
                **payload.model_copy(update={"verified_by": actor_id}).model_dump(),
                "status": "duplicate",
            }

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO canonical_records (
                person_id,
                household_id,
                record_type,
                field_name,
                value,
                verified_by,
                source_document_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.person_id,
                payload.household_id,
                payload.record_type,
                payload.field_name,
                payload.value,
                actor_id,
                payload.source_document_id,
            ),
        )

        if cur.rowcount == 0:
            existing = conn.execute(
                """SELECT id FROM canonical_records
                   WHERE person_id IS ? AND household_id IS ?
                     AND record_type=? AND field_name=? AND value=?
                     AND source_document_id IS ?""",
                (
                    payload.person_id,
                    payload.household_id,
                    payload.record_type,
                    payload.field_name,
                    payload.value,
                    payload.source_document_id,
                ),
            ).fetchone()
            if not existing:
                raise HTTPException(409, "Canonical record conflicts with an existing record")
            return {"id": existing["id"], **payload.model_dump(), "status": "duplicate"}

        record_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO audit_log (
                actor_id,
                action,
                entity_type,
                entity_id,
                details_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                "create_canonical_record",
                "canonical_record",
                str(record_id),
                json.dumps(
                    {
                        "field_name": payload.field_name,
                        "source_document_id": (
                            payload.source_document_id
                        ),
                    }
                ),
            ),
        )

        return {
            "id": record_id,
            **payload.model_copy(update={"verified_by": actor_id}).model_dump(),
            "status": "verified",
        }


@app.get("/people/{person_id}/records")
def get_person_canonical_records(
    person_id: int,
):
    with connection() as conn:
        person = conn.execute(
            "SELECT * FROM people WHERE id=?",
            (person_id,),
        ).fetchone()

        if not person:
            raise HTTPException(
                404,
                "Person not found",
            )

        rows = conn.execute(
            """
            SELECT
                cr.id,
                cr.record_type,
                cr.field_name,
                cr.value,
                cr.verified_by,
                cr.verified_at,
                cr.source_document_id,
                cr.created_at,
                cr.updated_at
            FROM canonical_records cr
            WHERE cr.person_id=?
            ORDER BY
                cr.record_type,
                cr.field_name,
                cr.id
            """,
            (person_id,),
        ).fetchall()

        return {
            "person": dict(person),
            "records": [
                dict(row)
                for row in rows
            ],
        }


@app.get("/people/{person_id}/timeline")
def person_timeline(
    person_id: int,
):
    with connection() as conn:
        person = conn.execute(
            "SELECT * FROM people WHERE id=?",
            (person_id,),
        ).fetchone()

        if not person:
            raise HTTPException(
                404,
                "Person not found",
            )

        rows = conn.execute(
            """
            SELECT
                id,
                event_type,
                event_date,
                details_json,
                verification_status,
                verified_by,
                verified_at,
                source_document_id,
                created_at
            FROM events
            WHERE person_id=?
            ORDER BY
                event_date DESC,
                id DESC
            """,
            (person_id,),
        ).fetchall()

        timeline = []

        for row in rows:
            timeline.append(
                {
                    "event_id": row["id"],
                    "event_type": row["event_type"],
                    "event_date": row["event_date"],
                    "details": json.loads(
                        row["details_json"] or "{}"
                    ),
                    "verified": (
                        row["verification_status"]
                        == "verified"
                    ),
                    "verified_by": row["verified_by"],
                    "verified_at": row["verified_at"],
                    "source_document_id": (
                        row["source_document_id"]
                    ),
                    "created_at": row["created_at"],
                }
            )

        return {
            "person": dict(person),
            "timeline": timeline,
        }
@app.get("/people/{person_id}/pregnancy-summary")
def pregnancy_summary(person_id: int):
    with connection() as conn:
        person = conn.execute(
            """
            SELECT *
            FROM people
            WHERE id=?
            """,
            (person_id,),
        ).fetchone()

        if not person:
            raise HTTPException(
                404,
                "Person not found",
            )

        # ---------------------------------------------------------
        # Pregnancy-level canonical records
        # ---------------------------------------------------------
        canonical_rows = conn.execute(
            """
            SELECT
                id,
                record_type,
                field_name,
                value,
                verified_by,
                verified_at,
                source_document_id
            FROM canonical_records
            WHERE person_id=?
              AND record_type IN (
                  'pregnancy',
                  'anc',
                  'pregnancy_record'
              )
            ORDER BY id
            """,
            (person_id,),
        ).fetchall()

        pregnancy_records = {}

        for row in canonical_rows:
            pregnancy_records[row["field_name"]] = {
                "value": row["value"],
                "verified": True,
                "verified_by": row["verified_by"],
                "verified_at": row["verified_at"],
                "source_document_id": row["source_document_id"],
                "record_id": row["id"],
            }

        # ---------------------------------------------------------
        # ANC events
        # ---------------------------------------------------------
        event_rows = conn.execute(
            """
            SELECT
                id,
                event_type,
                event_date,
                details_json,
                verification_status,
                verified_by,
                verified_at,
                source_document_id,
                created_at
            FROM events
            WHERE person_id=?
              AND event_type IN (
                  'anc_visit',
                  'anc'
              )
            ORDER BY
                event_date ASC,
                id ASC
            """,
            (person_id,),
        ).fetchall()

        anc_visits = []

        for row in event_rows:
            details = json.loads(
                row["details_json"] or "{}"
            )

            anc_visit_number = details.get("anc_visit")

            anc_visits.append(
                {
                    "event_id": row["id"],
                    "event_date": row["event_date"],
                    "anc_visit": anc_visit_number,
                    "blood_pressure": details.get(
                        "blood_pressure"
                    ),
                    "weight_kg": details.get(
                        "weight_kg"
                    ),
                    "hemoglobin": details.get(
                        "hemoglobin"
                    ),
                    "details": details,
                    "verified": (
                        row["verification_status"]
                        == "verified"
                    ),
                    "verified_by": row["verified_by"],
                    "verified_at": row["verified_at"],
                    "source_document_id": (
                        row["source_document_id"]
                    ),
                    "created_at": row["created_at"],
                }
            )

        # ---------------------------------------------------------
        # Visit-number analysis
        # ---------------------------------------------------------
        visit_numbers = sorted(
            {
                visit["anc_visit"]
                for visit in anc_visits
                if isinstance(
                    visit["anc_visit"],
                    int,
                )
                and visit["anc_visit"] > 0
            }
        )

        gaps = []

        if visit_numbers:
            for number in range(
                visit_numbers[0],
                visit_numbers[-1] + 1,
            ):
                if number not in visit_numbers:
                    gaps.append(number)

        # ---------------------------------------------------------
        # Latest ANC visit
        # ---------------------------------------------------------
        latest_visit = (
            anc_visits[-1]
            if anc_visits
            else None
        )

        # ---------------------------------------------------------
        # Latest measurements
        # ---------------------------------------------------------
        latest_measurements = {
            "blood_pressure": None,
            "weight_kg": None,
            "hemoglobin": None,
        }

        for visit in reversed(anc_visits):
            for field in latest_measurements:
                if (
                    latest_measurements[field]
                    is None
                    and visit.get(field) is not None
                ):
                    latest_measurements[field] = (
                        visit[field]
                    )

        # ---------------------------------------------------------
        # Record completeness
        # ---------------------------------------------------------
        has_lmp = (
            "lmp" in pregnancy_records
            and bool(
                pregnancy_records["lmp"]["value"]
            )
        )

        has_edd = (
            "edd" in pregnancy_records
            and bool(
                pregnancy_records["edd"]["value"]
            )
        )

        completeness = {
            "has_lmp": has_lmp,
            "has_edd": has_edd,
            "has_anc_visit": len(anc_visits) > 0,
            "has_latest_blood_pressure": (
                latest_measurements[
                    "blood_pressure"
                ]
                is not None
            ),
            "has_latest_weight": (
                latest_measurements["weight_kg"]
                is not None
            ),
            "has_latest_hemoglobin": (
                latest_measurements["hemoglobin"]
                is not None
            ),
        }

        return {
            "person": {
                "id": person["id"],
                "name": person["name"],
                "sex": person["sex"],
                "date_of_birth": person["date_of_birth"],
            },
            "pregnancy": {
                "lmp": pregnancy_records.get("lmp"),
                "edd": pregnancy_records.get("edd"),
            },
            "anc": {
                "visit_count": len(anc_visits),
                "recorded_visit_numbers": visit_numbers,
                "gaps_in_recorded_visit_numbers": gaps,
                "latest_visit": latest_visit,
                "latest_measurements": latest_measurements,
                "visits": anc_visits,
            },
            "record_completeness": completeness,
        }
