import uuid

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_pregnancy_summary_returns_anc_journey():
    run_id = uuid.uuid4().hex[:8]

    household_response = client.post(
        "/households",
        json={
            "external_id": f"TEST-PREG-HH-{run_id}",
            "village_id": "TEST-VILLAGE",
            "address": "Test Address",
        },
    )

    assert household_response.status_code == 200
    household_id = household_response.json()["id"]

    person_response = client.post(
        "/people",
        json={
            "external_id": f"TEST-PREG-PERSON-{run_id}",
            "household_id": household_id,
            "name": "Rukhsana",
            "sex": "F",
            "date_of_birth": "1999-01-01",
            "relationship_to_head": "self",
        },
    )

    assert person_response.status_code == 200
    person_id = person_response.json()["id"]

    lmp_response = client.post(
        "/canonical-records",
        json={
            "person_id": person_id,
            "household_id": household_id,
            "record_type": "pregnancy",
            "field_name": "lmp",
            "value": "2026-06-01",
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert lmp_response.status_code == 200

    edd_response = client.post(
        "/canonical-records",
        json={
            "person_id": person_id,
            "household_id": household_id,
            "record_type": "pregnancy",
            "field_name": "edd",
            "value": "2027-03-08",
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert edd_response.status_code == 200

    event_1 = client.post(
        "/events",
        json={
            "external_id": f"TEST-PREG-ANC-1-{run_id}",
            "household_id": household_id,
            "person_id": person_id,
            "asha_id": "TEST-ASHA-001",
            "event_type": "anc_visit",
            "event_date": "2026-07-10",
            "details": {
                "anc_visit": 1,
                "blood_pressure": "118/76",
                "weight_kg": 51.0,
            },
            "verification_status": "verified",
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert event_1.status_code == 200

    event_3 = client.post(
        "/events",
        json={
            "external_id": f"TEST-PREG-ANC-3-{run_id}",
            "household_id": household_id,
            "person_id": person_id,
            "asha_id": "TEST-ASHA-001",
            "event_type": "anc_visit",
            "event_date": "2026-08-20",
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

    assert event_3.status_code == 200

    summary_response = client.get(
        f"/people/{person_id}/pregnancy-summary"
    )

    assert summary_response.status_code == 200

    result = summary_response.json()

    assert result["person"]["name"] == "Rukhsana"

    assert result["pregnancy"]["lmp"]["value"] == (
        "2026-06-01"
    )

    assert result["pregnancy"]["edd"]["value"] == (
        "2027-03-08"
    )

    anc = result["anc"]

    assert anc["visit_count"] == 2
    assert anc["recorded_visit_numbers"] == [1, 3]
    assert anc["gaps_in_recorded_visit_numbers"] == [2]

    assert anc["latest_visit"]["anc_visit"] == 3
    assert anc["latest_visit"]["event_date"] == (
        "2026-08-20"
    )

    assert (
        anc["latest_measurements"]["blood_pressure"]
        == "120/80"
    )

    assert (
        anc["latest_measurements"]["weight_kg"]
        == 52.5
    )

    assert (
        anc["latest_measurements"]["hemoglobin"]
        == 11.2
    )

    completeness = result["record_completeness"]

    assert completeness["has_lmp"] is True
    assert completeness["has_edd"] is True
    assert completeness["has_anc_visit"] is True
    assert completeness["has_latest_blood_pressure"] is True
    assert completeness["has_latest_weight"] is True
    assert completeness["has_latest_hemoglobin"] is True