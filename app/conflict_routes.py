from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from .auth import Principal, actor_for_request, person_access_predicate, require_person_access
from .db import connection
from .document_conflicts import adjudicate_conflict, conflict_details
from .models import ConflictReview


router = APIRouter()


@router.get("/conflicts")
def list_conflicts(
    request: Request,
    status: Literal[
        "unresolved",
        "open",
        "needs_evidence",
        "resolved_source_a",
        "resolved_source_b",
        "resolved_external_evidence",
        "resolved_not_conflict",
    ] = "unresolved",
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    principal: Principal = request.state.principal
    with connection() as conn:
        scope, scope_params = person_access_predicate(
            principal, "c.person_id", "p.household_id"
        )
        where = []
        params: list = []
        if status == "unresolved":
            where.append("c.status IN ('open', 'needs_evidence')")
        else:
            where.append("c.status=?")
            params.append(status)
        if scope:
            where.append(scope.removeprefix(" AND "))
            params.extend(scope_params)
        predicate = " AND ".join(where)
        total = conn.execute(
            f"""SELECT COUNT(*) AS n
                FROM document_field_conflicts c
                JOIN people p ON p.id=c.person_id
                WHERE {predicate}""",
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT c.id
                FROM document_field_conflicts c
                JOIN people p ON p.id=c.person_id
                WHERE {predicate}
                ORDER BY c.created_at, c.id
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        items = []
        for row in rows:
            item = conflict_details(conn, row["id"])
            item.pop("history", None)
            items.append(item)
        conn.execute(
            """INSERT INTO audit_log (
                   actor_id, action, entity_type, entity_id, details_json
               ) VALUES (?, 'list_document_conflicts', 'document_conflict', '*', ?)""",
            (
                principal.subject,
                f'{{"status":"{status}","limit":{limit},"offset":{offset}}}',
            ),
        )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/conflicts/{conflict_id}")
def get_conflict(conflict_id: int, request: Request):
    principal: Principal = request.state.principal
    with connection() as conn:
        result = conflict_details(conn, conflict_id)
        require_person_access(conn, principal, result["person_id"])
        conn.execute(
            """INSERT INTO audit_log (
                   actor_id, action, entity_type, entity_id, details_json
               ) VALUES (?, 'read_document_conflict', 'document_conflict', ?, '{}')""",
            (principal.subject, str(conflict_id)),
        )
        return result


@router.post("/conflicts/{conflict_id}/review")
def review_conflict(
    conflict_id: int,
    payload: ConflictReview,
    request: Request,
):
    actor_id = actor_for_request(request)
    with connection() as conn:
        conflict = conn.execute(
            "SELECT person_id, status FROM document_field_conflicts WHERE id=?",
            (conflict_id,),
        ).fetchone()
        if conflict is None:
            raise HTTPException(404, "Conflict not found.")
        require_person_access(conn, request.state.principal, conflict["person_id"])
        if conflict["status"] not in {"open", "needs_evidence"}:
            raise HTTPException(
                409,
                "Only open conflicts or conflicts awaiting evidence can be reviewed.",
            )
        reason = payload.reason.strip()
        if len(reason) < 5:
            raise HTTPException(
                422,
                "A review reason of at least five non-space characters is required.",
            )
        return adjudicate_conflict(
            conn,
            conflict_id,
            actor_id,
            payload.decision,
            reason,
            payload.evidence_field_id,
        )
