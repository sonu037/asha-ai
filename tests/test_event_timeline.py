import uuid

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_verified_anc_event_appears_in_household_timeline():
    run_id = uuid.uuid4().hex[:8]

    # ---------------------------------------------------------
    # 1. Create household
    # ---------------------------------------------------------
    household_response = client.post(
        "/households",
        json={
            "external_id": f"TEST-TIMELINE-HH-{run_id}",
            "village_id": "TEST-VILLAGE",
            "address": "Test Address",
        },
    )

    assert household_response.status_code == 200

    household_id = household_response.json()["id"]

    # ---------------------------------------------------------
    # 2. Create person
    # ---------------------------------------------------------
    person_response = client.post(
        "/people",
        json={
            "external_id": f"TEST-TIMELINE-PERSON-{run_id}",
            "household_id": household_id,
            "name": "Rukhsana",
            "sex": "F",
            "date_of_birth": "1999-01-01",
            "relationship_to_head": "self",
        },
    )

    assert person_response.status_code == 200

    person_id = person_response.json()["id"]

    # ---------------------------------------------------------
    # 3. Create verified ANC clinical event
    # ---------------------------------------------------------
    event_response = client.post(
        "/events",
        json={
            "external_id": f"TEST-ANC-EVENT-{run_id}",
            "household_id": household_id,
            "person_id": person_id,
            "asha_id": "TEST-ASHA-001",
            "event_type": "anc_visit",
            "event_date": "2026-08-18",
            "details": {
                "anc_visit": 3,
                "blood_pressure": "120/80",
                "weight_kg": 52.5,
                "hemoglobin": 11.2,
            },
            "source_document_id": None,
            "verification_status": "verified",
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert event_response.status_code == 200

    event = event_response.json()

    assert event["event_type"] == "anc_visit"
    assert event["event_date"] == "2026-08-18"
    assert event["verification_status"] == "verified"
    assert event["verified_by"] == "TEST-ASHA-001"

    # ---------------------------------------------------------
    # 4. Retrieve household timeline
    # ---------------------------------------------------------
    timeline_response = client.get(
        f"/households/{household_id}/timeline"
    )

    assert timeline_response.status_code == 200

    timeline = timeline_response.json()

    assert len(timeline) >= 1

    anc_event = next(
        item
        for item in timeline
        if item["event_type"] == "anc_visit"
    )

    # ---------------------------------------------------------
    # 5. Verify timeline preserved clinical details
    # ---------------------------------------------------------
    assert anc_event["event_date"] == "2026-08-18"
    assert anc_event["verified"] is True
    assert anc_event["verified_by"] == "TEST-ASHA-001"

    assert anc_event["details"]["anc_visit"] == 3
    assert anc_event["details"]["blood_pressure"] == "120/80"
    assert anc_event["details"]["weight_kg"] == 52.5
    assert anc_event["details"]["hemoglobin"] == 11.2