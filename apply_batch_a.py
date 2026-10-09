        link = conn.execute(
            "SELECT * FROM document_person_links WHERE document_id=?", (document_id,)
        ).fetchone()
        if not link:
            raise HTTPException(409, "Document is not linked to a person")

        fields = _fields(conn, document_id)
        event_type = payload.event_type or _EVENT_TYPE_MAP.get(doc["document_type"], doc["document_type"] or "document_event")
        event_date = payload.event_date or fields.get("event_date", {}).get("value") or fields.get("visit_date", {}).get("value") or fields.get("date", {}).get("value")
        details = dict(payload.details)
        for name, item in fields.items():
            if item["value"] not in (None, "") and name not in details:
                details[name] = item["value"]
        details["source"] = "verified_document"
        details["document_id"] = document_id

        duplicate = conn.execute(
            """SELECT * FROM events WHERE person_id=? AND event_type=?\n               AND (? IS NULL OR event_date=?) AND source_document_id=?""",
            (link["person_id"], event_type, event_date, event_date, document_id),
        ).fetchone()
        if duplicate:
            return {"status": "duplicate", "event_id": duplicate["id"], "document_id": document_id, "person_id": link["person_id"], "event_type": event_type}

        external_id = f"DOC-EVENT-{document_id}-{uuid.uuid4().hex[:10].upper()}"
        cur = conn.execute(
            """INSERT INTO events(external_id,person_id,event_type,event_date,details_json,source_document_id,verification_status)\n               VALUES(?,?,?,?,?,?,'verified')""",
            (external_id, link["person_id"], event_type, event_date, json.dumps(details), document_id),
        )
        event_id = cur.lastrowid
        conn.execute(
            """INSERT INTO audit_log(actor_id,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?)""",
            (payload.verified_by, "auto_create_event", "event", str(event_id), json.dumps({"document_id": document_id, "person_id": link["person_id"]})),
        )
        return {"status": "created", "event_id": event_id, "document_id": document_id, "person_id": link["person_id"], "event_type": event_type, "event_date": event_date, "verification_status": "verified"}


@router.get("/search")
def unified_search(q: str = Query(..., min_length=2), limit: int = Query(20, ge=1, le=100)):
    """Search people, households, documents and events from one endpoint."""
    needle = f"%{q.strip()}%"
    with connection() as conn:
        people = conn.execute(
            """SELECT p.id, p.name, p.external_id, p.household_id, h.village_id\n               FROM people p LEFT JOIN households h ON h.id=p.household_id\n               WHERE p.name LIKE ? OR p.external_id LIKE ? OR h.village_id LIKE ?\n               ORDER BY p.id DESC LIMIT ?""", (needle, needle, needle, limit)
        ).fetchall()
        docs = conn.execute(
            """SELECT id, external_id, original_filename, document_type, verification_status\n               FROM documents WHERE original_filename LIKE ? OR document_type LIKE ? OR ocr_text LIKE ?\n               ORDER BY id DESC LIMIT ?""", (needle, needle, needle, limit)
        ).fetchall()
        events = conn.execute(
            """SELECT e.id,e.person_id,e.event_type,e.event_date,e.verification_status,p.name\n               FROM events e LEFT JOIN people p ON p.id=e.person_id\n               WHERE e.event_type LIKE ? OR e.event_date LIKE ? OR e.details_json LIKE ? OR p.name LIKE ?\n               ORDER BY e.id DESC LIMIT ?""", (needle, needle, needle, needle, limit)
        ).fetchall()
        return {"query": q, "people": [dict(r) for r in people], "documents": [dict(r) for r in docs], "events": [dict(r) for r in events]}


@router.get("/people/{person_id}/health-summary")
def health_summary(person_id: int):
    """Unified read-only longitudinal summary. It does not diagnose."""
    with connection() as conn:
        person = conn.execute(
            """SELECT p.*,h.village_id,h.address FROM people p LEFT JOIN households h ON h.id=p.household_id WHERE p.id=?""", (person_id,)
        ).fetchone()
        if not person:
            raise HTTPException(404, "Person not found")
        events = conn.execute("SELECT * FROM events WHERE person_id=? ORDER BY event_date DESC,id DESC", (person_id,)).fetchall()
        docs = conn.execute(
            """SELECT d.id,d.document_type,d.original_filename,d.verification_status,d.created_at\n               FROM documents d JOIN document_person_links l ON l.document_id=d.id WHERE l.person_id=? ORDER BY d.id DESC""", (person_id,)
        ).fetchall()
        by_type: dict[str, int] = {}
        for e in events:
            by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1
        latest = []
        for e in events[:10]:
            latest.append({"id": e["id"], "event_type": e["event_type"], "event_date": e["event_date"], "verified": e["verification_status"] == "verified", "details": json.loads(e["details_json"] or "{}")})
        return {
            "person": dict(person),
            "event_counts": by_type,
            "event_count": len(events),
            "verified_event_count": sum(e["verification_status"] == "verified" for e in events),
            "linked_document_count": len(docs),
            "latest_events": latest,
            "linked_documents": [dict(d) for d in docs],
            "flags": {"has_unverified_events": any(e["verification_status"] != "verified" for e in events)},
            "disclaimer": "This is a record summary, not a diagnosis or clinical decision.",
        }
''', encoding='utf-8')

# ---------- main.py router registration ----------
main = MAIN.read_text(encoding='utf-8')
if 'from .batch_a import router as batch_a_router' not in main:
    main = main.replace('from .ai import provider\n', 'from .ai import provider\nfrom .batch_a import router as batch_a_router\n', 1)
if 'app.include_router(batch_a_router)' not in main:
    marker = "app = FastAPI(title='ASHA AI', version='0.2.0', description='AI-first ASHA documentation and record intelligence platform.')\n"
    if marker not in main:
        raise SystemExit('Could not locate FastAPI app declaration in app/main.py')
    main = main.replace(marker, marker + "app.include_router(batch_a_router)\n", 1)
MAIN.write_text(main, encoding='utf-8')

print('Batch A patch applied in-place:')
print('  - duplicate document/event detection')
print('  - stronger person matching')
print('  - unified search')
print('  - automatic verified document -> event creation')
print('  - unified person health summary')
print('  - DB migration for content_hash + document-person links')
print('Now run: pytest -q')