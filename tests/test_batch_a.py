import hashlib
import sqlite3
import uuid
import warnings

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import connection, init_db
from app.main import app
from app.schema import ExtractedFact, ExtractionEnvelope


@pytest.fixture
def client():
    init_db()
    with TestClient(app) as test_client:
        yield test_client


def create_person(client, name="Nadia", village="Village A"):
    token = uuid.uuid4().hex
    household = client.post(
        "/households",
        json={
            "external_id": f"BA-HH-{token}",
            "village_id": village,
            "address": "Address",
        },
    ).json()
    person = client.post(
        "/people",
        json={
            "external_id": f"BA-P-{token}",
            "household_id": household["id"],
            "name": name,
        },
    ).json()
    return household, person


def upload(client, data, filename="same.png"):
    return client.post(
        "/documents",
        files={"file": (filename, data, "image/png")},
    )


def add_field(document_id, field_name, value, verified=True, needs_review=False):
    with connection() as conn:
        conn.execute(
            """INSERT INTO document_fields (
                   document_id, field_name, extracted_value, verified_value,
                   confidence, extraction_method, needs_review
               ) VALUES (?, ?, ?, ?, 1.0, 'manual', ?)""",
            (
                document_id,
                field_name,
                value,
                value if verified else None,
                int(needs_review),
            ),
        )


def link_and_verify(document_id, person_id):
    with connection() as conn:
        conn.execute(
            "UPDATE documents SET verification_status='verified' WHERE id=?",
            (document_id,),
        )
        conn.execute(
            "INSERT INTO document_person_links(document_id, person_id, linked_by) VALUES (?, ?, ?)",
            (document_id, person_id, "reviewer"),
        )


def test_same_upload_bytes_are_deduplicated_but_filename_is_not_enough(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(
            document_type="unknown", extraction_status="failed", warnings=["OCR unavailable"]
        ),
    )
    token = uuid.uuid4().hex.encode()
    first_bytes = b"unique bytes one " + token
    first = upload(client, first_bytes)
    same_content = upload(client, first_bytes, filename="renamed.png")
    different_content = upload(client, b"unique bytes two " + token)

    assert first.status_code == 200
    assert first.json()["status"] == "created"
    assert same_content.json()["status"] == "duplicate"
    assert same_content.json()["document_id"] == first.json()["document_id"]
    stored = client.get(f"/documents/{first.json()['document_id']}").json()["document"]
    assert stored["content_hash"] == hashlib.sha256(first_bytes).hexdigest()
    assert different_content.json()["status"] == "created"
    assert different_content.json()["document_id"] != first.json()["document_id"]


def test_verified_event_requires_all_fields_verified_and_link(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="anc_record", extraction_status="failed"),
    )
    _, person = create_person(client)
    document_id = upload(client, b"event bytes " + uuid.uuid4().hex.encode()).json()["document_id"]
    add_field(document_id, "visit_date", "2026-01-10", verified=False)
    link_and_verify(document_id, person["id"])

    response = client.post(
        f"/documents/{document_id}/create-verified-event",
        json={
            "person_id": person["id"],
            "event_type": "anc_visit",
            "verified_by": "reviewer",
        },
    )

    assert response.status_code == 409


def test_final_field_verification_automatically_creates_anc_event(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: ExtractionEnvelope(
            document_type="anc_record",
            facts=[
                ExtractedFact(
                    field_name="anc_visit",
                    value=2,
                    confidence=1.0,
                    method="rule",
                ),
                ExtractedFact(
                    field_name="visit_date",
                    value="2026-04-12",
                    confidence=1.0,
                    method="rule",
                ),
            ],
        ),
    )
    _, person = create_person(client)
    document_id = upload(
        client, b"verified ANC auto event " + uuid.uuid4().hex.encode()
    ).json()["document_id"]
    fields = client.get(f"/documents/{document_id}").json()["fields"]
    linked = client.post(
        f"/documents/{document_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    )
    assert linked.status_code == 200

    first = client.post(
        f"/documents/{document_id}/fields/{fields[0]['id']}/verify",
        json={"verified_value": str(fields[0]["extracted_value"]), "verified_by": "reviewer"},
    )
    assert first.json()["document_status"] == "partially_verified"
    assert first.json()["automatic_event"]["status"] == "not_ready"

    final = client.post(
        f"/documents/{document_id}/fields/{fields[1]['id']}/verify",
        json={"verified_value": fields[1]["extracted_value"], "verified_by": "reviewer"},
    )
    assert final.json()["document_status"] == "verified"
    assert final.json()["automatic_event"]["status"] == "created"

    repeated = client.post(
        f"/documents/{document_id}/fields/{fields[1]['id']}/verify",
        json={"verified_value": fields[1]["extracted_value"], "verified_by": "reviewer"},
    )
    assert repeated.json()["automatic_event"]["status"] == "duplicate"
    timeline = client.get(f"/people/{person['id']}/timeline").json()["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["event_type"] == "anc_visit"
    assert timeline[0]["event_date"] == "2026-04-12"
    assert timeline[0]["details"]["anc_visit"] == "2"
    assert timeline[0]["source_document_id"] == document_id


def test_verified_event_is_idempotent_and_distinct_details_are_preserved(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="anc_record", extraction_status="failed"),
    )
    _, person = create_person(client)
    document_id = upload(client, b"unique event bytes " + uuid.uuid4().hex.encode()).json()["document_id"]
    add_field(document_id, "anc_visit", "3", verified=True)
    link_and_verify(document_id, person["id"])
    payload = {
        "person_id": person["id"],
        "event_type": "anc_visit",
        "event_date": "2026-01-10",
        "details": {"visit": 1},
        "verified_by": "reviewer",
    }

    first = client.post(f"/documents/{document_id}/create-verified-event", json=payload)
    duplicate = client.post(f"/documents/{document_id}/create-verified-event", json=payload)
    distinct = client.post(
        f"/documents/{document_id}/create-verified-event",
        json={**payload, "details": {"visit": 2}},
    )

    assert first.json()["status"] == "created"
    assert duplicate.json()["status"] == "duplicate"
    assert distinct.json()["status"] == "created"
    with connection() as conn:
        first_event = conn.execute(
            "SELECT details_json, source_document_id FROM events WHERE id=?",
            (first.json()["event_id"],),
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source_document_id=?",
            (document_id,),
        ).fetchone()[0]
    assert count == 2
    assert '"anc_visit":"3"' in first_event["details_json"]
    assert first_event["source_document_id"] == document_id


def test_verified_event_requires_link_and_transaction_rolls_back_audit_failure(
    client, monkeypatch
):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="anc_record", extraction_status="failed"),
    )
    _, person = create_person(client)
    document_id = upload(client, b"rollback event bytes " + uuid.uuid4().hex.encode()).json()["document_id"]
    with connection() as conn:
        conn.execute("UPDATE documents SET verification_status='verified' WHERE id=?", (document_id,))
    payload = {
        "person_id": person["id"],
        "event_type": "anc_visit",
        "event_date": "2026-01-10",
        "details": {"visit": 1},
        "verified_by": "reviewer",
    }
    assert client.post(
        f"/documents/{document_id}/create-verified-event", json=payload
    ).status_code == 409

    link_and_verify(document_id, person["id"])
    from app import batch_a

    real_connection = batch_a.connection

    class FailAuditConnection:
        def __enter__(self):
            self.context = real_connection()
            self.conn = self.context.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                return self.context.__exit__(exc_type, exc, traceback)
            self.context.__exit__(exc_type, exc, traceback)
            return False

        def execute(self, sql, params=()):
            if "INSERT INTO audit_log" in sql:
                raise RuntimeError("simulated audit failure")
            return self.conn.execute(sql, params)

    monkeypatch.setattr(batch_a, "connection", FailAuditConnection)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        client.post(f"/documents/{document_id}/create-verified-event", json=payload)
    with connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source_document_id=?",
            (document_id,),
        ).fetchone()[0]
    assert count == 0


def test_verified_event_rejects_empty_payload_when_document_has_no_verified_fields(
    client, monkeypatch
):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="anc_record", extraction_status="failed"),
    )
    _, person = create_person(client)
    document_id = upload(
        client, b"empty verified document " + uuid.uuid4().hex.encode()
    ).json()["document_id"]
    link_and_verify(document_id, person["id"])

    response = client.post(
        f"/documents/{document_id}/create-verified-event",
        json={
            "person_id": person["id"],
            "event_type": "anc_visit",
            "verified_by": "reviewer",
        },
    )

    assert response.status_code == 422


def test_search_filters_by_person_and_spans_records_without_returning_ocr(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="visit", extraction_status="failed"),
    )
    household_a, person_a = create_person(client, "Nadia")
    household_b, person_b = create_person(client, "Nadia")
    doc_a = upload(client, b"search A " + uuid.uuid4().hex.encode()).json()["document_id"]
    doc_b = upload(client, b"search B " + uuid.uuid4().hex.encode()).json()["document_id"]
    with connection() as conn:
        for document_id, person in ((doc_a, person_a), (doc_b, person_b)):
            conn.execute(
                "INSERT INTO document_person_links(document_id, person_id, linked_by) VALUES (?, ?, ?)",
                (document_id, person["id"], "reviewer"),
            )
        conn.execute(
            """INSERT INTO canonical_records (
                   person_id, household_id, record_type, field_name, value,
                   verified_by, source_document_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (person_a["id"], household_a["id"], "screening", "test_field", "needle", "reviewer", doc_a),
        )
        conn.execute(
            """INSERT INTO events (
                   external_id, household_id, person_id, event_type, event_date,
                   details_json, source_document_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (f"BA-E-{uuid.uuid4().hex}", household_a["id"], person_a["id"], "needle_visit", "2026-01-01", "{}", doc_a),
        )

    response = client.get("/search", params={"q": "Nadia", "person_id": person_a["id"]})
    assert response.status_code == 200
    result = response.json()
    assert [row["id"] for row in result["people"]] == [person_a["id"]]
    assert [row["id"] for row in result["documents"]] == [doc_a]
    assert len(result["canonical_records"]) == 1
    assert len(result["events"]) == 1
    assert "ocr_text" not in response.text
    filtered = client.get(
        "/search",
        params={
            "q": "Nadia",
            "person_id": person_a["id"],
            "record_type": "screening",
        },
    ).json()
    assert len(filtered["canonical_records"]) == 1
    assert filtered["events"] == []
    no_results = client.get("/search", params={"q": "zz-not-here"})
    assert no_results.status_code == 200
    assert no_results.json()["events"] == []
    invalid = client.get("/search", params={"q": "xx", "date_from": "2026-02-01", "date_to": "2026-01-01"})
    assert invalid.status_code == 422


def test_person_matching_normalizes_names_and_marks_ambiguity_for_review(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="unknown", extraction_status="failed"),
    )
    name = f"{uuid.uuid4().hex} Khan"
    _, first = create_person(client, f"  {name.upper()}  ")
    _, second = create_person(client, name)
    document_id = upload(client, b"match bytes " + uuid.uuid4().hex.encode()).json()["document_id"]
    add_field(document_id, "name", f" {name.casefold()} ", verified=True)

    response = client.get(f"/documents/{document_id}/match-candidates")
    result = response.json()

    assert response.status_code == 200
    assert result["match_status"] == "ambiguous"
    assert result["ambiguous"] is True
    assert result["review_required"] is True
    assert result["auto_assign"] is False
    assert {item["person_id"] for item in result["candidates"]} == {
        first["id"],
        second["id"],
    }


def test_person_matching_exact_and_conflicting_identifier_cases(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="unknown", extraction_status="failed"),
    )
    _, person = create_person(client, f"Unique {uuid.uuid4().hex}")
    document_id = upload(
        client, b"exact match " + uuid.uuid4().hex.encode()
    ).json()["document_id"]
    with connection() as conn:
        external_id = conn.execute(
            "SELECT external_id FROM people WHERE id=?", (person["id"],)
        ).fetchone()["external_id"]
    add_field(document_id, "name", f"  {person['name'].swapcase()} ", verified=True)
    exact = client.get(f"/documents/{document_id}/match-candidates").json()
    assert exact["match_status"] == "exact"
    assert exact["candidates"][0]["person_id"] == person["id"]

    conflicting_document = upload(
        client, b"conflicting id " + uuid.uuid4().hex.encode()
    ).json()["document_id"]
    add_field(conflicting_document, "name", person["name"], verified=True)
    add_field(
        conflicting_document,
        "person_external_id",
        f"NOT-{external_id}",
        verified=True,
    )
    conflicting = client.get(
        f"/documents/{conflicting_document}/match-candidates"
    ).json()
    assert conflicting["match_status"] == "no_match"
    assert conflicting["candidates"] == []


def test_health_summary_is_person_scoped_and_includes_provenance(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="unknown", extraction_status="failed"),
    )
    household_a, person_a = create_person(client, "Person A")
    household_b, person_b = create_person(client, "Person B")
    doc_a = upload(client, b"summary A " + uuid.uuid4().hex.encode()).json()["document_id"]
    doc_b = upload(client, b"summary B " + uuid.uuid4().hex.encode()).json()["document_id"]
    with connection() as conn:
        for document_id, person in ((doc_a, person_a), (doc_b, person_b)):
            conn.execute(
                "INSERT INTO document_person_links(document_id, person_id, linked_by) VALUES (?, ?, ?)",
                (document_id, person["id"], "reviewer"),
            )
        conn.execute(
            """INSERT INTO events (
                   external_id, household_id, person_id, event_type, event_date,
                   details_json, source_document_id, verification_status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"BA-E-{uuid.uuid4().hex}", household_a["id"], person_a["id"], "visit", "2026-01-01", "{}", doc_a, "unverified"),
        )
        conn.execute(
            """INSERT INTO canonical_records (
                   person_id, household_id, record_type, field_name, value,
                   verified_by, source_document_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (person_a["id"], household_a["id"], "screening", "test", "value", "reviewer", doc_a),
        )

    response = client.get(f"/people/{person_a['id']}/health-summary")
    result = response.json()
    assert response.status_code == 200
    assert result["event_count"] == 1
    assert result["events"][0]["review_required"] is True
    assert result["events"][0]["source_document_id"] == doc_a
    assert len(result["canonical_records"]) == 1
    assert [doc["id"] for doc in result["linked_documents"]] == [doc_a]
    assert client.get(f"/people/{person_b['id']}/health-summary").json()["event_count"] == 0


def test_duplicate_canonical_record_is_returned_without_creating_second(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.tesseract_provider.extract",
        lambda _path: __import__("app.schema", fromlist=["ExtractionEnvelope"])
        .ExtractionEnvelope(document_type="unknown", extraction_status="failed"),
    )
    household, person = create_person(client)
    payload = {
        "person_id": person["id"],
        "household_id": household["id"],
        "record_type": "identity",
        "field_name": "name",
        "value": "Nadia",
        "verified_by": "reviewer",
    }
    first = client.post("/canonical-records", json=payload)
    second = client.post("/canonical-records", json=payload)
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "duplicate"
    records = client.get(f"/people/{person['id']}/records").json()["records"]
    assert len([record for record in records if record["field_name"] == "name"]) == 1


def test_db_init_is_repeatable_for_fresh_and_legacy_databases(tmp_path, monkeypatch):
    from app import db

    fresh_path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB_PATH", fresh_path)
    init_db()
    init_db()
    with connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(documents)")
        }
    assert "content_hash" in columns
    assert "idx_documents_content_hash" in indexes

    legacy_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(legacy_path)
    legacy.execute(
        """CREATE TABLE documents (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               external_id TEXT UNIQUE NOT NULL,
               document_type TEXT,
               original_filename TEXT,
               storage_path TEXT NOT NULL,
               captured_at TEXT,
               ocr_text TEXT,
               language TEXT,
               extraction_status TEXT NOT NULL DEFAULT 'pending',
               verification_status TEXT NOT NULL DEFAULT 'unverified',
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    legacy.commit()
    legacy.close()
    monkeypatch.setattr(db, "DB_PATH", legacy_path)
    init_db()
    init_db()
    with connection() as conn:
        legacy_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(documents)")
        }
        legacy_indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(documents)")
        }
    assert "content_hash" in legacy_columns
    assert "idx_documents_content_hash" in legacy_indexes


def test_migration_preserves_existing_duplicates_and_warns(tmp_path, monkeypatch):
    from app import db

    duplicate_path = tmp_path / "legacy-duplicates.db"
    legacy = sqlite3.connect(duplicate_path)
    legacy.executescript(db.SCHEMA)
    household_id = legacy.execute(
        "INSERT INTO households(external_id) VALUES ('HH-DUP')"
    ).lastrowid
    person_id = legacy.execute(
        "INSERT INTO people(external_id, household_id, name) VALUES ('P-DUP', ?, 'Test')",
        (household_id,),
    ).lastrowid
    document_id = legacy.execute(
        """INSERT INTO documents(external_id, storage_path, content_hash)
           VALUES ('DUP-1', 'unused', 'same-hash')"""
    ).lastrowid
    legacy.execute(
        "INSERT INTO documents(external_id, storage_path, content_hash) VALUES ('DUP-2', 'unused', 'same-hash')"
    )
    for external_id in ("EV-DUP-1", "EV-DUP-2"):
        legacy.execute(
            """INSERT INTO events (
                   external_id, person_id, event_type, event_date,
                   details_json, source_document_id
               ) VALUES (?, ?, 'visit', '2026-01-01', '{}', ?)""",
            (external_id, person_id, document_id),
        )
        legacy.execute(
            """INSERT INTO canonical_records (
                   person_id, household_id, record_type, field_name, value,
                   verified_by, source_document_id
               ) VALUES (?, ?, 'screening', 'field', 'value', 'reviewer', ?)""",
            (person_id, household_id, document_id),
        )
    legacy.commit()
    legacy.close()
    monkeypatch.setattr(db, "DB_PATH", duplicate_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        init_db()
    assert len(caught) == 3
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM canonical_records").fetchone()[0] == 2
