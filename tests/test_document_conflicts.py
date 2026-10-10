from pathlib import Path
import hashlib
import json
import uuid

from fastapi.testclient import TestClient

from app.classification import classify_document
from app.db import connection
from app.extraction import extract_basic_facts
from app.main import app
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


def _make_conflict(client, monkeypatch, values=("110/70", "150/90")):
    person = _create_person(client)
    transcripts = {}
    for index, value in enumerate(values):
        body = f"synthetic-review-{uuid.uuid4().hex}-{index}".encode()
        transcripts[body] = (
            "ANC Record\nANC Visit: 2\nVisit Date: 18/08/2026\n"
            f"Blood Pressure: {value}\nWeight: 50 kg"
        )
    monkeypatch.setattr(
        "app.main.get_provider",
        lambda: SyntheticTranscriptProvider(transcripts),
    )
    document_ids = [_upload(client, body) for body in transcripts]
    for document_id in document_ids:
        linked = client.post(
            f"/documents/{document_id}/link-person",
            json={"person_id": person["id"], "linked_by": "synthetic-reviewer"},
        )
        assert linked.status_code == 200, linked.text
    with connection() as conn:
        fields = {}
        for document_id in document_ids:
            fields[document_id] = {
                row["field_name"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM document_fields WHERE document_id=?",
                    (document_id,),
                ).fetchall()
            }
    conflict = client.get("/conflicts").json()["items"][0]
    return person, document_ids, fields, conflict


def _set_verified_state(document_ids, fields):
    with connection() as conn:
        for document_id in document_ids:
            conn.execute(
                """UPDATE documents
                   SET verification_status='verified',
                       classification_needs_review=0
                   WHERE id=?""",
                (document_id,),
            )
            for field in fields[document_id].values():
                conn.execute(
                    """UPDATE document_fields
                       SET verified_value=extracted_value,
                           verified_by='synthetic-reviewer',
                           verified_at=CURRENT_TIMESTAMP,
                           needs_review=0
                       WHERE id=?""",
                    (field["id"],),
                )


def test_conflict_queue_and_all_document_fact_promotion_paths_are_guarded(
    client, monkeypatch
):
    person, document_ids, fields, conflict = _make_conflict(client, monkeypatch)
    assert conflict["status"] == "open"
    _set_verified_state(document_ids, fields)

    field_id = fields[document_ids[0]]["blood_pressure"]["id"]
    verify = client.post(
        f"/documents/{document_ids[0]}/fields/{field_id}/verify",
        json={"verified_value": "110/70", "verified_by": "reviewer"},
    )
    assert verify.status_code == 409

    for document_id, value in zip(document_ids, ("110/70", "150/90")):
        event_body = {
            "person_id": person["id"],
            "event_type": "anc_visit",
            "event_date": "2026-08-18",
            "details": {"blood_pressure": value},
            "verified_by": "reviewer",
        }
        assert client.post(
            f"/documents/{document_id}/create-event", json=event_body
        ).status_code == 409
        assert client.post(
            f"/documents/{document_id}/create-verified-event",
            json=event_body,
        ).status_code == 409
        assert client.post(
            "/events",
            json={
                **event_body,
                "external_id": f"CONFLICT-EVENT-{uuid.uuid4().hex}",
                "source_document_id": document_id,
                "verification_status": "verified",
            },
        ).status_code == 409

    assert client.post(
        "/events",
        json={
            "external_id": f"CONFLICT-EVENT-{uuid.uuid4().hex}",
            "person_id": person["id"],
            "event_type": "anc_visit",
            "event_date": "2026-08-18",
            "details": {"blood_pressure": "110/70"},
            "verification_status": "verified",
        },
    ).status_code == 409

    assert client.post(
        "/canonical-records",
        json={
            "person_id": person["id"],
            "record_type": "anc",
            "field_name": "blood_pressure",
            "value": "110/70",
            "verified_by": "reviewer",
            "source_document_id": document_ids[0],
        },
    ).status_code == 409
    assert client.post(
        "/canonical-records",
        json={
            "person_id": person["id"],
            "record_type": "anc",
            "field_name": "blood_pressure",
            "value": "110/70",
            "verified_by": "reviewer",
        },
    ).status_code == 409

    from app.batch_a import auto_create_event_from_verified_document
    from fastapi import HTTPException

    with connection() as conn:
        try:
            auto_create_event_from_verified_document(
                conn, document_ids[0], "reviewer"
            )
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("Automatic event creation must honor open conflicts.")


def test_adjudication_preserves_history_and_does_not_verify_other_fields(
    client, monkeypatch
):
    person, document_ids, fields, conflict = _make_conflict(client, monkeypatch)
    assert client.get("/conflicts").json()["total"] == 1
    detail = client.get(f"/conflicts/{conflict['id']}").json()
    assert {source["value"] for source in detail["sources"]} == {
        "110/70",
        "150/90",
    }
    assert {source["source_text"] for source in detail["sources"]} == {
        "Blood Pressure: 110/70",
        "Blood Pressure: 150/90",
    }

    selected_id = fields[document_ids[0]]["blood_pressure"]["id"]
    unrelated_id = fields[document_ids[0]]["weight_kg"]["id"]
    resolution = client.post(
        f"/conflicts/{conflict['id']}/review",
        json={
            "decision": "resolve_source_a",
            "reason": "Source A matches the signed ANC source.",
        },
    )
    assert resolution.status_code == 200, resolution.text
    result = resolution.json()
    assert result["status"] == "resolved_source_a"
    assert result["selected_field_id"] == selected_id
    assert result["resolved_by"] == "development"
    assert result["resolved_at"]
    assert result["history"][-1]["reason"] == (
        "Source A matches the signed ANC source."
    )

    with connection() as conn:
        selected = conn.execute(
            "SELECT verified_value, needs_review FROM document_fields WHERE id=?",
            (selected_id,),
        ).fetchone()
        unrelated = conn.execute(
            "SELECT verified_value FROM document_fields WHERE id=?",
            (unrelated_id,),
        ).fetchone()
        audit = conn.execute(
            """SELECT actor_id, action FROM audit_log
               WHERE entity_type='document_conflict' AND entity_id=?
               ORDER BY id DESC LIMIT 1""",
            (str(conflict["id"]),),
        ).fetchone()
    assert selected["verified_value"] is None
    assert selected["needs_review"] == 1
    assert unrelated["verified_value"] is None
    assert audit["actor_id"] == "development"
    assert audit["action"] == "resolve_source_a"

    assert client.post(
        f"/documents/{document_ids[0]}/fields/{selected_id}/verify",
        json={"verified_value": "110/70", "verified_by": "reviewer"},
    ).status_code == 200
    assert client.post(
        f"/documents/{document_ids[1]}/fields/"
        f"{fields[document_ids[1]]['blood_pressure']['id']}/verify",
        json={"verified_value": "150/90", "verified_by": "reviewer"},
    ).status_code == 409
    weight_value = fields[document_ids[0]]["weight_kg"]["extracted_value"]
    assert client.post(
        f"/documents/{document_ids[0]}/fields/{unrelated_id}/verify",
        json={"verified_value": weight_value, "verified_by": "reviewer"},
    ).status_code == 200
    with connection() as conn:
        unrelated = conn.execute(
            "SELECT verified_value FROM document_fields WHERE id=?",
            (unrelated_id,),
        ).fetchone()
    assert unrelated["verified_value"] == weight_value
    assert client.post(
        f"/conflicts/{conflict['id']}/review",
        json={"decision": "request_evidence", "reason": "Transition check."},
    ).status_code == 409


def test_review_authorization_blocks_cross_person_access(
    client, monkeypatch
):
    person, _document_ids, _fields, conflict = _make_conflict(client, monkeypatch)
    other_person = _create_person(client)
    token = "scoped-review-token"
    entries = [
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "subject": "other-reviewer",
            "role": "asha",
            "person_ids": [other_person["id"]],
            "household_ids": [],
        }
    ]
    monkeypatch.setenv("ASHA_ENV", "production")
    monkeypatch.setenv("ASHA_AUTH_TOKENS_JSON", json.dumps(entries))
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/conflicts", headers=headers).json()["total"] == 0
    assert client.get(
        f"/conflicts/{conflict['id']}", headers=headers
    ).status_code == 404
    assert client.post(
        f"/conflicts/{conflict['id']}/review",
        headers=headers,
        json={"decision": "not_conflict", "reason": "Different person."},
    ).status_code == 404

    token = "authorized-review-token"
    entries[0]["token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    entries[0]["subject"] = "person-reviewer"
    entries[0]["person_ids"] = [person["id"]]
    monkeypatch.setenv("ASHA_AUTH_TOKENS_JSON", json.dumps(entries))
    allowed = client.post(
        f"/conflicts/{conflict['id']}/review",
        headers={"Authorization": f"Bearer {token}"},
        json={"decision": "request_evidence", "reason": "Need signed record."},
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "needs_evidence"
    assert allowed.json()["resolved_by"] is None


def test_new_contradictory_evidence_reopens_resolution_and_keeps_history(
    client, monkeypatch
):
    person, _document_ids, _fields, conflict = _make_conflict(client, monkeypatch)
    resolved = client.post(
        f"/conflicts/{conflict['id']}/review",
        json={
            "decision": "resolve_source_a",
            "reason": "Signed record supports source A.",
        },
    )
    assert resolved.status_code == 200

    body = f"new-conflicting-{uuid.uuid4().hex}".encode()
    transcripts = {
        body: (
            "ANC Record\nANC Visit: 2\nVisit Date: 18/08/2026\n"
            "Blood Pressure: 150/90\nWeight: 50 kg"
        )
    }
    monkeypatch.setattr(
        "app.main.get_provider",
        lambda: SyntheticTranscriptProvider(transcripts),
    )
    third_id = _upload(client, body)
    assert client.post(
        f"/documents/{third_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    ).status_code == 200

    reopened = client.get(f"/conflicts/{conflict['id']}").json()
    assert reopened["status"] == "open"
    assert reopened["resolution_decision"] is None
    assert reopened["history"][-1]["decision"] == "conflict_reopened"
    assert reopened["history"][-1]["details"]["new_document_id"] == third_id
    assert client.get("/conflicts").json()["total"] >= 1


def test_further_evidence_can_resolve_all_conflicts_for_the_visit(
    client, monkeypatch
):
    person, _document_ids, _fields, conflict = _make_conflict(client, monkeypatch)
    requested = client.post(
        f"/conflicts/{conflict['id']}/review",
        json={"decision": "request_evidence", "reason": "Need another source."},
    )
    assert requested.status_code == 200
    assert requested.json()["status"] == "needs_evidence"

    body = f"new-support-{uuid.uuid4().hex}".encode()
    transcripts = {
        body: (
            "ANC Record\nANC Visit: 2\nVisit Date: 18/08/2026\n"
            "Blood Pressure: 120/80\nWeight: 50 kg"
        )
    }
    monkeypatch.setattr(
        "app.main.get_provider",
        lambda: SyntheticTranscriptProvider(transcripts),
    )
    third_id = _upload(client, body)
    assert client.post(
        f"/documents/{third_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    ).status_code == 200
    detail = client.get(f"/conflicts/{conflict['id']}").json()
    candidate = next(
        item
        for item in detail["candidate_sources"]
        if item["document_id"] == third_id and item["value"] == "120/80"
    )
    resolution = client.post(
        f"/conflicts/{conflict['id']}/review",
        json={
            "decision": "resolve_with_evidence",
            "evidence_field_id": candidate["field_id"],
            "reason": "New linked source supports this value.",
        },
    )
    assert resolution.status_code == 200, resolution.text
    assert resolution.json()["status"] == "resolved_external_evidence"
    assert resolution.json()["selected_field_id"] == candidate["field_id"]
    assert client.get("/conflicts").json()["total"] == 0


def test_invalid_decision_and_database_failure_roll_back_adjudication(
    client, monkeypatch
):
    _person, _document_ids, _fields, conflict = _make_conflict(client, monkeypatch)
    assert client.post(
        f"/conflicts/{conflict['id']}/review",
        json={"decision": "not_conflict", "reason": "     "},
    ).status_code == 422

    with connection() as conn:
        conn.execute(
            """CREATE TRIGGER reject_conflict_review
               BEFORE INSERT ON document_conflict_reviews
               BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END"""
        )
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        failure = failing_client.post(
            f"/conflicts/{conflict['id']}/review",
            json={
                "decision": "not_conflict",
                "reason": "Synthetic transaction rollback check.",
            },
        )
    assert failure.status_code == 500
    with connection() as conn:
        row = conn.execute(
            "SELECT status FROM document_field_conflicts WHERE id=?",
            (conflict["id"],),
        ).fetchone()
        history_count = conn.execute(
            "SELECT COUNT(*) FROM document_conflict_reviews WHERE conflict_id=?",
            (conflict["id"],),
        ).fetchone()[0]
    assert row["status"] == "open"
    assert history_count == 1
