import uuid

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_verified_field_can_be_promoted_to_canonical_record():
    run_id = uuid.uuid4().hex[:8]
    # ---------------------------------------------------------
    # 1. Create household
    # ---------------------------------------------------------
    household_response = client.post(
        "/households",
        json={
            "external_id": f"TEST-HH-CANONICAL-{run_id}",
            "village_id": "TEST-VILLAGE",
            "address": "Test Address",
        },
    )

    assert household_response.status_code == 200
    household = household_response.json()
    household_id = household["id"]

    # ---------------------------------------------------------
    # 2. Create person
    # ---------------------------------------------------------
    person_response = client.post(
        "/people",
        json={
            "external_id": f"TEST-PERSON-CANONICAL-{run_id}",
            "household_id": household_id,
            "name": "Rukhsana",
            "sex": "F",
            "date_of_birth": "1999-01-01",
            "relationship_to_head": "self",
        },
    )

    assert person_response.status_code == 200
    person = person_response.json()
    person_id = person["id"]

    # ---------------------------------------------------------
    # 3. Upload the test ASHA document
    # ---------------------------------------------------------
    with open("test_asha_record.png", "rb") as image:
        document_response = client.post(
            "/documents",
            files={
                "file": (
                    "test_asha_record.png",
                    image,
                    "image/png",
                )
            },
        )

    assert document_response.status_code == 200

    document = document_response.json()
    document_id = document["document_id"]

    # ---------------------------------------------------------
    # 4. Retrieve extracted fields
    # ---------------------------------------------------------
    document_detail_response = client.get(
        f"/documents/{document_id}"
    )

    assert document_detail_response.status_code == 200

    document_detail = document_detail_response.json()
    fields = document_detail["fields"]

    name_field = next(
        field
        for field in fields
        if field["field_name"] == "name"
    )

    assert name_field["extracted_value"] == "Rukhsana"

    # ---------------------------------------------------------
    # 5. Human verifies extracted field
    # ---------------------------------------------------------
    verify_response = client.post(
        f"/documents/{document_id}/fields/"
        f"{name_field['id']}/verify",
        json={
            "verified_value": "Rukhsana",
            "verified_by": "TEST-ASHA-001",
        },
    )

    assert verify_response.status_code == 200

    verification = verify_response.json()

    assert verification["status"] == "verified"
    assert verification["field_id"] == name_field["id"]

    # ---------------------------------------------------------
    # 6. Promote verified value to canonical record
    # ---------------------------------------------------------
    canonical_response = client.post(
        "/canonical-records",
        json={
            "person_id": person_id,
            "household_id": household_id,
            "record_type": "person_identity",
            "field_name": "name",
            "value": "Rukhsana",
            "verified_by": "TEST-ASHA-001",
            "source_document_id": document_id,
        },
    )

    assert canonical_response.status_code == 200

    canonical = canonical_response.json()

    assert canonical["status"] == "verified"
    assert canonical["person_id"] == person_id
    assert canonical["household_id"] == household_id
    assert canonical["field_name"] == "name"
    assert canonical["value"] == "Rukhsana"
    assert canonical["verified_by"] == "TEST-ASHA-001"
    assert canonical["source_document_id"] == document_id