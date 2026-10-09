import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .db import connection


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    person_ids: frozenset[int] = frozenset()
    household_ids: frozenset[int] = frozenset()
    can_create_households: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="A valid Bearer token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _production_principals() -> list[tuple[str, Principal]]:
    raw_config = os.environ.get("ASHA_AUTH_TOKENS_JSON")
    if not raw_config:
        raise HTTPException(
            status_code=503,
            detail="Production authentication is not configured.",
        )
    try:
        entries = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=503,
            detail="ASHA_AUTH_TOKENS_JSON must contain valid JSON.",
        ) from exc
    if not isinstance(entries, list) or not entries:
        raise HTTPException(
            status_code=503,
            detail="ASHA_AUTH_TOKENS_JSON must be a non-empty list.",
        )

    principals = []
    subjects = set()
    hashes = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=503,
                detail="Each authentication entry must be a JSON object.",
            )
        token_hash = entry.get("token_sha256")
        subject = entry.get("subject")
        role = entry.get("role", "asha")
        if (
            not isinstance(token_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", token_hash)
            or not isinstance(subject, str)
            or not subject.strip()
            or not isinstance(role, str)
            or role not in {"admin", "asha"}
        ):
            raise HTTPException(
                status_code=503,
                detail="Authentication entries require a SHA-256 token hash, subject, and valid role.",
            )
        if subject in subjects or token_hash.lower() in hashes:
            raise HTTPException(
                status_code=503,
                detail="Authentication subjects and token hashes must be unique.",
            )
        subjects.add(subject)
        hashes.add(token_hash.lower())
        person_ids = entry.get("person_ids", [])
        household_ids = entry.get("household_ids", [])
        can_create_households = entry.get("can_create_households", False)
        if (
            not isinstance(person_ids, list)
            or any(type(value) is not int or value < 1 for value in person_ids)
            or not isinstance(household_ids, list)
            or any(type(value) is not int or value < 1 for value in household_ids)
            or type(can_create_households) is not bool
        ):
            raise HTTPException(
                status_code=503,
                detail=(
                    "person_ids and household_ids must be lists of positive "
                    "integers, and can_create_households must be a boolean."
                ),
            )
        if role == "admin" and (person_ids or household_ids):
            raise HTTPException(
                status_code=503,
                detail="Admin principals use unrestricted access, not scoped ID lists.",
            )
        principals.append(
            (
                token_hash.lower(),
                Principal(
                    subject=subject,
                    role=role,
                    person_ids=frozenset(person_ids),
                    household_ids=frozenset(household_ids),
                    can_create_households=can_create_households,
                ),
            )
        )
    return principals


def authenticate(request: Request) -> Principal:
    environment = os.environ.get("ASHA_ENV", "development").strip().lower()
    if environment == "development":
        principal = Principal(subject="development", role="admin")
        request.state.principal = principal
        return principal
    if environment not in {"production", "staging"}:
        raise HTTPException(
            status_code=503,
            detail="ASHA_ENV must be development, staging, or production.",
        )

    configured_principals = _production_principals()
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        raise _unauthorized()
    token_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    matched_principal = None
    for configured_hash, principal in configured_principals:
        if hmac.compare_digest(token_hash, configured_hash):
            matched_principal = principal
    if matched_principal is None:
        raise _unauthorized()
    request.state.principal = matched_principal
    return matched_principal


def _has_person_access(conn, principal: Principal, person_id: int) -> bool:
    if principal.is_admin or person_id in principal.person_ids:
        return True
    row = conn.execute(
        "SELECT household_id FROM people WHERE id=?",
        (person_id,),
    ).fetchone()
    return bool(row and row["household_id"] in principal.household_ids)


def require_person_access(
    conn,
    principal: Principal,
    person_id: int,
) -> None:
    if not _has_person_access(conn, principal, person_id):
        raise HTTPException(status_code=404, detail="Person not found.")


def person_access_predicate(
    principal: Principal,
    person_expression: str,
    household_expression: str,
) -> tuple[str, list[int]]:
    if principal.is_admin:
        return "", []
    conditions = []
    params: list[int] = []
    if principal.person_ids:
        person_ids = sorted(principal.person_ids)
        conditions.append(
            f"{person_expression} IN ({','.join('?' for _ in person_ids)})"
        )
        params.extend(person_ids)
    if principal.household_ids:
        household_ids = sorted(principal.household_ids)
        conditions.append(
            f"{household_expression} IN ({','.join('?' for _ in household_ids)})"
        )
        params.extend(household_ids)
    if not conditions:
        return " AND 1=0", []
    return f" AND ({' OR '.join(conditions)})", params


def require_household_access(
    principal: Principal,
    household_id: int,
) -> None:
    if not principal.is_admin and household_id not in principal.household_ids:
        raise HTTPException(status_code=404, detail="Household not found.")


def require_document_access(
    conn,
    principal: Principal,
    document_id: int,
) -> None:
    if principal.is_admin:
        return
    row = conn.execute(
        """SELECT d.created_by, l.person_id
           FROM documents d
           LEFT JOIN document_person_links l ON l.document_id=d.id
           WHERE d.id=?""",
        (document_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found.")
    if row["person_id"] is not None:
        require_person_access(conn, principal, row["person_id"])
    elif row["created_by"] != principal.subject:
        raise HTTPException(status_code=404, detail="Document not found.")


def actor_for_request(request: Request, claimed_subject: str | None = None) -> str:
    principal: Principal = request.state.principal
    if os.environ.get("ASHA_ENV", "development").strip().lower() == "development":
        return claimed_subject or principal.subject
    return principal.subject


def _positive_id(value, name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value):
        parsed = int(value)
    else:
        raise HTTPException(
            status_code=422,
            detail=f"{name} must be a positive integer.",
        )
    if parsed < 1:
        raise HTTPException(status_code=422, detail=f"{name} must be a positive integer.")
    return parsed


async def _request_payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object.")
    return payload


async def authorize_request(request: Request) -> None:
    if request.url.path == "/health":
        return
    principal = authenticate(request)
    path = request.url.path

    params = request.path_params
    method = request.method.upper()
    person_id = params.get("person_id")
    household_id = params.get("household_id")
    document_id = params.get("document_id")
    if person_id is not None:
        with connection() as conn:
            require_person_access(conn, principal, _positive_id(person_id, "person_id"))
    if household_id is not None:
        require_household_access(principal, _positive_id(household_id, "household_id"))
    if document_id is not None:
        with connection() as conn:
            require_document_access(conn, principal, _positive_id(document_id, "document_id"))

    if path == "/households" and method == "POST":
        if not principal.is_admin and not principal.can_create_households:
            raise HTTPException(status_code=403, detail="Household creation is not permitted.")
    elif path == "/people" and method == "POST":
        payload = await _request_payload(request)
        target_household_id = _positive_id(
            payload.get("household_id"), "household_id"
        )
        with connection() as conn:
            require_household_access(principal, target_household_id)
    elif path in {"/events", "/canonical-records"} and method == "POST":
        payload = await _request_payload(request)
        if not principal.is_admin and (
            payload.get("person_id") is None
            and payload.get("household_id") is None
        ):
            raise HTTPException(
                status_code=403,
                detail="A scoped person or household reference is required.",
            )
        if (
            path == "/canonical-records"
            and not principal.is_admin
            and payload.get("source_document_id") is None
        ):
            raise HTTPException(
                status_code=403,
                detail="Scoped users may create canonical records only from a source document.",
            )
        with connection() as conn:
            if payload.get("person_id") is not None:
                require_person_access(
                    conn,
                    principal,
                    _positive_id(payload["person_id"], "person_id"),
                )
            if payload.get("household_id") is not None:
                require_household_access(
                    principal,
                    _positive_id(payload["household_id"], "household_id"),
                )
            if payload.get("source_document_id") is not None:
                require_document_access(
                    conn,
                    principal,
                    _positive_id(payload["source_document_id"], "source_document_id"),
                )
    elif document_id is not None and method in {"POST", "PUT", "PATCH"}:
        if path.endswith("/link-person") or path.endswith("/create-event") or path.endswith("/create-verified-event"):
            payload = await _request_payload(request)
            if payload.get("person_id") is not None:
                with connection() as conn:
                    require_person_access(
                        conn,
                        principal,
                        _positive_id(payload["person_id"], "person_id"),
                    )

    if path == "/search":
        requested_person_id = request.query_params.get("person_id")
        if requested_person_id is not None:
            with connection() as conn:
                require_person_access(
                    conn,
                    principal,
                    _positive_id(requested_person_id, "person_id"),
                )
        requested_household_id = request.query_params.get("household_id")
        if requested_household_id is not None:
            require_household_access(
                principal,
                _positive_id(requested_household_id, "household_id"),
            )

    if method == "GET" or (method == "POST" and path.endswith("/assistant")):
        if person_id is not None:
            entity_type, entity_id = "person", str(person_id)
        elif household_id is not None:
            entity_type, entity_id = "household", str(household_id)
        elif document_id is not None:
            entity_type, entity_id = "document", str(document_id)
        elif path == "/search":
            entity_type, entity_id = "search", "query"
        elif path == "/households":
            entity_type, entity_id = "household", "list"
        else:
            return
        with connection() as conn:
            conn.execute(
                """INSERT INTO audit_log (
                       actor_id, action, entity_type, entity_id, details_json
                   ) VALUES (?, 'authorized_read', ?, ?, '{}')""",
                (principal.subject, entity_type, entity_id),
            )
