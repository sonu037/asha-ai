from pathlib import Path
import uuid

from app.classification import classify_document
from app.extraction import extract_basic_facts
from app.schema import ExtractionEnvelope


class SyntheticTranscriptProvider:
    name = "synthetic_conflict_test"
    model = None

    def __init__(self, transcripts):
        self.transcripts = transcripts

    def extract(self, path):
        text = self.transcripts[Path(path).read_bytes()]
        classification = classify_document(text)
        return ExtractionEnvelope(
            document_type=classification.document_type,
            classification_confidence=classification.confidence,
            classification_needs_review=classification.needs_review,
            raw_text=text,
            facts=extract_basic_facts(text),
            warnings=(
                [classification.reason]
                if classification.ambiguous and classification.reason
                else []
            ),
            provider=self.name,
            extraction_status="extracted",
        )


def _create_person(client):
    token = uuid.uuid4().hex
    household = client.post(
        "/households",
        json={"external_id": f"CONFLICT-HH-{token}", "village_id": "Synthetic"},
    ).json()
    person = client.post(
        "/people",
        json={
            "external_id": f"CONFLICT-P-{token}",
            "household_id": household["id"],
            "name": "Synthetic Person",
        },
    ).json()
    return person


def _upload(client, content):
    response = client.post(
        "/documents",
        files={"file": (f"{uuid.uuid4().hex}.png", content, "image/png")},
    )
    assert response.status_code == 200
    document_id = response.json()["document_id"]
    return document_id


def test_same_person_same_visit_conflicts_keep_both_source_references(
    client, monkeypatch
):
    person = _create_person(client)
    other_person = _create_person(client)
    transcripts = {
        b"synthetic-conflict-a": (
            "ANC Record\nANC Visit: 2\nVisit Date: 18/08/2026\n"
            "Blood Pressure: 110/70"
        ),
        b"synthetic-conflict-b": (
            "ANC Record\nANC Visit: 2\nVisit Date: 18/08/2026\n"
            "Blood Pressure: 150/90"
        ),
        b"synthetic-different-date": (
            "ANC Record\nANC Visit: 2\nVisit Date: 19/08/2026\n"
            "Blood Pressure: 150/90"
        ),
        b"synthetic-other-person": (
            "ANC Record\nANC Visit: 2\nVisit Date: 18/08/2026\n"
            "Blood Pressure: 150/90"
        ),
        b"synthetic-ambiguous-date": (
            "ANC Record\nANC Visit: 2\nVisit Date: 03/04/2026\n"
            "Blood Pressure: 150/90"
        ),
    }
    monkeypatch.setattr(
        "app.main.get_provider",
        lambda: SyntheticTranscriptProvider(transcripts),
    )

    first_id = _upload(client, b"synthetic-conflict-a")
    second_id = _upload(client, b"synthetic-conflict-b")
    first_link = client.post(
        f"/documents/{first_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    )
    second_link = client.post(
        f"/documents/{second_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    )
    assert first_link.status_code == 200
    assert first_link.json()["conflicts"] == []
    assert second_link.status_code == 200

    conflicts = second_link.json()["conflicts"]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["field_name"] == "blood_pressure"
    assert conflict["status"] == "open"
    assert {
        source["document_id"] for source in conflict["sources"]
    } == {first_id, second_id}
    assert {
        source["value"] for source in conflict["sources"]
    } == {"110/70", "150/90"}
    assert {
        source["source_text"] for source in conflict["sources"]
    } == {"Blood Pressure: 110/70", "Blood Pressure: 150/90"}

    for document_id in (first_id, second_id):
        stored = client.get(f"/documents/{document_id}").json()
        assert len(stored["conflicts"]) == 1
        pressure = next(
            field
            for field in stored["fields"]
            if field["field_name"] == "blood_pressure"
        )
        assert pressure["needs_review"] == 1
        assert "cross-document conflict" in pressure["review_reason"].casefold()

    different_date_id = _upload(client, b"synthetic-different-date")
    different_date_link = client.post(
        f"/documents/{different_date_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    )
    assert different_date_link.status_code == 200
    assert different_date_link.json()["conflicts"] == []

    other_person_id = _upload(client, b"synthetic-other-person")
    other_person_link = client.post(
        f"/documents/{other_person_id}/link-person",
        json={"person_id": other_person["id"], "linked_by": "reviewer"},
    )
    assert other_person_link.status_code == 200
    assert other_person_link.json()["conflicts"] == []

    ambiguous_date_id = _upload(client, b"synthetic-ambiguous-date")
    ambiguous_date_link = client.post(
        f"/documents/{ambiguous_date_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    )
    assert ambiguous_date_link.status_code == 200
    assert ambiguous_date_link.json()["conflicts"] == []
