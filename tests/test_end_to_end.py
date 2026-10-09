import io
import uuid
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_end_to_end_core_workflow(client):
    # 1. Health
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # 2. Create household
    household = {
        "external_id": f"E2E-HH-{id(client)}",
        "village_id": None,
        "address": "Test Village",
    }

    r = client.post("/households", json=household)
    assert r.status_code == 200, r.text

    household_id = r.json()["id"]

    # 3. Create person
    person = {
        "external_id": f"E2E-P-{id(client)}",
        "household_id": household_id,
        "name": "Test Person",
        "sex": "F",
        "date_of_birth": "2000-01-01",
        "relationship_to_head": "head",
    }

    r = client.post("/people", json=person)
    assert r.status_code == 200, r.text

    person_id = r.json()["id"]

    # 4. Upload a sample image.
    # v0.2 does not have real OCR/vision yet.
    sample = f"ASHA TEST DOCUMENT {uuid.uuid4()}".encode()

    r = client.post(
        "/documents",
        files={
            "file": (
                "sample.jpg",
                io.BytesIO(sample),
                "image/jpeg",
            )
        },
    )

    assert r.status_code == 200, r.text

    document = r.json()
    document_id = document["document_id"]

    # 5. Confirm document was safely stored.
    r = client.get(f"/documents/{document_id}")
    assert r.status_code == 200, r.text

    stored = r.json()

    assert stored["document"]["id"] == document_id
    assert stored["document"]["extraction_status"] == "failed"

    # v0.2 intentionally has no real OCR/vision provider.
    assert stored["fields"] == []

    # 6. Create an event linked to the household/person/document.
    event = {
        "external_id": f"E2E-EVENT-{id(client)}",
        "household_id": household_id,
        "person_id": person_id,
        "asha_id": "E2E-ASHA-001",
        "event_type": "home_visit",
        "event_date": "2026-10-06",
        "details": {
            "purpose": "automated end-to-end test"
        },
        "source_document_id": document_id,
    }

    r = client.post("/events", json=event)
    assert r.status_code == 200, r.text

    # 7. Confirm the event appears in the household timeline.
    r = client.get(f"/households/{household_id}/timeline")
    assert r.status_code == 200, r.text

    timeline = r.json()

    assert any(
        item["event_type"] == "home_visit"
        and item["source_document_id"] == document_id
        for item in timeline
    )

    # 8. Confirm person retrieval.
    r = client.get(f"/people/{person_id}")
    assert r.status_code == 200, r.text

    assert r.json()["id"] == person_id