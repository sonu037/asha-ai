import uuid

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_person_timeline_returns_verified_events():
    run_id = uuid.uuid4().hex[:8]

    household_response = client.post(
        "/households",
        json={
            "external_id": f"TEST-PERSON-TIMELINE-HH-{run_id}",
            "village_id": "TEST-VILLAGE",
            "address": "Test Address",
        },
    )

    assert household_response.status_code == 200

    household_id = household_response.json()["id"]

    person_response = client.post(
        "/people",
        json={
            "external_id": f"TEST-PERSON-TIMELINE-{run_id}",
            "household_id": household_id,
            "name": "Rukhsana",
            "sex": "F",
            "date_of_birth": "1999-01-01",
            "relationship_to_head": "self",
        },
    )

    assert person_response.status_code == 200

    person_id = person_response.json()["id"]

    event_1 = client.post(
        "/events",
        json={
            "external_id": f"TEST-ANC-1-{run_id}",
            "household_id": household_id,
            "person_id": person_id,
            "asha_id": "TEST-ASHA-001",
            "event_type": "anc_visit",
            "event_date": "2026-07-20",
            "details": {
                "anc_visit": 2,
                "blood_pressure": "118/78",
                "weight_kg": 51.5,
            },
            "verification_status": "verified",
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert event_1.status_code == 200

    event_2 = client.post(
        "/events",
        json={
            "external_id": f"TEST-ANC-2-{run_id}",
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
            "verification_status": "verified",
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert event_2.status_code == 200

    timeline_response = client.get(
        f"/people/{person_id}/timeline"
    )

    assert timeline_response.status_code == 200

    result = timeline_response.json()

    assert result["person"]["id"] == person_id
    assert result["person"]["name"] == "Rukhsana"

    timeline = result["timeline"]

    assert len(timeline) == 2

    assert timeline[0]["event_date"] == "2026-08-18"
    assert timeline[0]["event_type"] == "anc_visit"
    assert timeline[0]["verified"] is True

    assert (
        timeline[0]["details"]["blood_pressure"]
        == "120/80"
    )

    assert (
        timeline[0]["details"]["weight_kg"]
        == 52.5
    )

    assert (
        timeline[0]["details"]["hemoglobin"]
        == 11.2
    )

    assert timeline[1]["event_date"] == "2026-07-20"
    assert timeline[1]["event_type"] == "anc_visit"
    assert timeline[1]["verified"] is True