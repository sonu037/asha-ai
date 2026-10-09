import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.db import connection


client = TestClient(app)


def test_verified_document_creates_person_event():
    run_id = uuid.uuid4().hex[:8]

    household_response = client.post(
        "/households",
        json={
            "external_id": f"TEST-DOC-EVENT-HH-{run_id}",
            "village_id": "TEST-VILLAGE",
            "address": "Test Address",
        },
    )

    assert household_response.status_code == 200
    household_id = household_response.json()["id"]

    person_response = client.post(
        "/people",
        json={
            "external_id": f"TEST-DOC-EVENT-PERSON-{run_id}",
            "household_id": household_id,
            "name": "Rukhsana",
            "sex": "F",
            "date_of_birth": "1999-01-01",
            "relationship_to_head": "self",
        },
    )

    assert person_response.status_code == 200
    person_id = person_response.json()["id"]

    document_response = client.post(
        "/documents",
        files={
            "file": (
                "verified_anc.png",
                b"fake image content",
                "image/png",
            )
        },
    )

    assert document_response.status_code == 200
    document_id = document_response.json()["document_id"]

    # Make the document fully verified for this test.
    with connection() as conn:
        conn.execute(
            """
            UPDATE documents
            SET verification_status='verified'
            WHERE id=?
            """,
            (document_id,),
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
                person_id,
                "TEST-ASHA-001",
            ),
        )

    event_response = client.post(
        f"/documents/{document_id}/create-event",
        json={
            "person_id": person_id,
            "event_type": "anc_visit",
            "event_date": "2026-09-15",
            "details": {
                "anc_visit": 4,
                "blood_pressure": "118/78",
                "weight_kg": 53.0,
            },
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert event_response.status_code == 200

    result = event_response.json()

    assert result["status"] == "created"
    assert result["document_id"] == document_id
    assert result["person_id"] == person_id
    assert result["event_type"] == "anc_visit"
    assert result["verification_status"] == "verified"

    timeline_response = client.get(
        f"/people/{person_id}/timeline"
    )

    assert timeline_response.status_code == 200

    timeline = timeline_response.json()["timeline"]

    assert len(timeline) == 1
    assert timeline[0]["event_type"] == "anc_visit"
    assert timeline[0]["event_date"] == "2026-09-15"
    assert timeline[0]["verified"] is True
    assert timeline[0]["source_document_id"] == document_id
    assert timeline[0]["details"]["anc_visit"] == 4