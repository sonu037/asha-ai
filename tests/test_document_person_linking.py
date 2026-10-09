import uuid

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_document_person_candidate_matching_and_linking():
    run_id = uuid.uuid4().hex[:8]

    household_response = client.post(
        "/households",
        json={
            "external_id": f"TEST-LINK-HH-{run_id}",
            "village_id": "Mantrigam",
            "address": "Test Address",
        },
    )

    assert household_response.status_code == 200
    household_id = household_response.json()["id"]

    person_response = client.post(
        "/people",
        json={
            "external_id": f"TEST-LINK-PERSON-{run_id}",
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
                "test_record.png",
                b"fake image content",
                "image/png",
            )
        },
    )

    assert document_response.status_code == 200

    document_id = document_response.json()["document_id"]

    # Add the extracted identity fields directly so this test
    # focuses on linking rather than OCR.
    from app.db import connection

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO document_fields (
                document_id,
                field_name,
                extracted_value,
                confidence,
                extraction_method
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                document_id,
                "name",
                "Rukhsana",
                0.95,
                "rule",
            ),
        )

        conn.execute(
            """
            INSERT INTO document_fields (
                document_id,
                field_name,
                extracted_value,
                confidence,
                extraction_method
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                document_id,
                "village",
                "Mantrigam",
                0.95,
                "rule",
            ),
        )

    candidate_response = client.get(
        f"/documents/{document_id}/person-candidates"
    )

    assert candidate_response.status_code == 200

    result = candidate_response.json()

    assert result["extracted_name"] == "Rukhsana"
    assert result["extracted_village"] == "Mantrigam"

    candidates = result["candidates"]

    assert len(candidates) >= 1

    best = candidates[0]

    assert best["person_id"] == person_id
    assert best["name"] == "Rukhsana"
    assert best["match_score"] == 1.0
    assert "name" in best["matched_fields"]
    assert "village" in best["matched_fields"]

    link_response = client.post(
        f"/documents/{document_id}/link-person",
        json={
            "person_id": person_id,
            "linked_by": "TEST-ASHA-001",
        },
    )

    assert link_response.status_code == 200

    linked = link_response.json()

    assert linked["status"] == "linked"
    assert linked["document_id"] == document_id
    assert linked["person_id"] == person_id
    assert linked["linked_by"] == "TEST-ASHA-001"

    duplicate_response = client.post(
        f"/documents/{document_id}/link-person",
        json={
            "person_id": person_id,
            "linked_by": "TEST-ASHA-001",
        },
    )

    assert duplicate_response.status_code == 409