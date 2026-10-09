import json
import uuid

import httpx

from app.assistant import answer_question
from app.classification import classify_document
from app.db import connection
from app.document_intelligence import OpenAICompatibleDocumentProvider
from app.extraction import extract_basic_facts
from app.main import app
from app.schema import ExtractionEnvelope
from app.auth import Principal
from fastapi.testclient import TestClient


def _create_person(client, name):
    token = uuid.uuid4().hex
    household = client.post(
        "/households",
        json={"external_id": f"AI-HH-{token}", "village_id": "AI-Village"},
    ).json()
    person = client.post(
        "/people",
        json={
            "external_id": f"AI-P-{token}",
            "household_id": household["id"],
            "name": name,
        },
    ).json()
    return person


def test_synthetic_document_classification_evaluation():
    cases = (
        ("ANC visit register, antenatal check", "anc_record"),
        ("Immunization and vaccination record, BCG", "immunization_record"),
        ("Home visit date and visit conducted", "home_visit"),
        ("NCD screening blood pressure", "ncd_screening"),
        ("Pregnancy record with LMP", "pregnancy_record"),
        ("Village Health and Nutrition Day (VHND)", "vhnd_record"),
        ("Drug kit stock quantity received", "inventory_record"),
        ("ASHA work diary entry", "asha_diary"),
        ("unreadable symbols only", "unknown"),
    )

    correct = sum(classify_document(text).document_type == label for text, label in cases)

    assert correct / len(cases) >= 0.875
    assert classify_document("").needs_review
    assert classify_document("ANC visit register").needs_review
    assert classify_document("ANC visit register").confidence <= 0.5
    ambiguous = classify_document("ANC antenatal immunization vaccination record")
    assert ambiguous.document_type == "unknown"
    assert ambiguous.needs_review
    assert classify_document("fancy dress record").document_type == "unknown"


def test_synthetic_field_extraction_requires_literal_evidence():
    facts = extract_basic_facts(
        "Name: Asha Example\nANC Visit: 2\nVisit Date: 12/04/2026\n"
        "Blood Pressure: 120/80\n"
    )
    by_name = {fact.field_name: fact for fact in facts}

    assert by_name["name"].value == "Asha Example"
    assert by_name["name"].source_text == "Name: Asha Example"
    assert by_name["anc_visit"].value == 2
    assert by_name["visit_date"].value == "12/04/2026"
    assert by_name["blood_pressure"].value == "120/80"
    assert extract_basic_facts("Name: \nANC Visit: 99\n") == []


def test_date_candidates_reject_invalid_dates_and_flag_ambiguity():
    impossible = extract_basic_facts("Visit Date: 39/19/2026")
    assert len(impossible) == 1
    assert impossible[0].field_name == "visit_date"
    assert impossible[0].value == "39/19/2026"
    assert impossible[0].needs_review is True
    assert "invalid calendar date" in impossible[0].reason.casefold()

    leap_day = extract_basic_facts("Visit Date: 29/02/2024")
    assert leap_day[0].value == "29/02/2024"
    assert "invalid calendar date" not in leap_day[0].reason.casefold()

    non_leap_day = extract_basic_facts("Visit Date: 29/02/2025")
    assert "invalid calendar date" in non_leap_day[0].reason.casefold()

    ambiguous = extract_basic_facts("Visit Date: 03/04/2026")
    assert ambiguous[0].value == "03/04/2026"
    assert ambiguous[0].needs_review is True
    assert "ambiguous" in ambiguous[0].reason.casefold()

    day_first = extract_basic_facts("Visit Date: 13/04/2026")
    month_first = extract_basic_facts("Visit Date: 04/13/2026")
    assert "ambiguous" not in day_first[0].reason.casefold()
    assert "ambiguous" not in month_first[0].reason.casefold()

    same_day_month = extract_basic_facts("Visit Date: 04/04/2026")
    assert "ambiguous" not in same_day_month[0].reason.casefold()

    two_digit_year = extract_basic_facts("Visit Date: 13/04/26")
    assert "two-digit year" in two_digit_year[0].reason.casefold()
    impossible_two_digit = extract_basic_facts("Visit Date: 39/19/26")
    assert "invalid calendar date" in impossible_two_digit[0].reason.casefold()


def test_overlapping_document_signals_are_explicitly_ambiguous():
    anc_with_shared_measurement = classify_document(
        "ANC Record\nANC Visit: 2\nBlood Pressure: 120/80"
    )
    assert anc_with_shared_measurement.document_type == "anc_record"

    overlap = classify_document("ANC and immunization vaccination record")
    assert overlap.document_type == "unknown"
    assert overlap.needs_review is True
    assert overlap.ambiguous is True
    assert "anc_record" in overlap.reason
    assert "immunization_record" in overlap.reason

    unknown = classify_document("unrelated blank ledger")
    assert unknown.document_type == "unknown"
    assert unknown.ambiguous is False
    assert unknown.needs_review is True


def test_invalid_or_ambiguous_dates_cannot_be_verified_without_correction(
    client, monkeypatch
):
    class DateFixtureProvider:
        def __init__(self, text):
            self.text = text

        def extract(self, _path):
            classification = classify_document(self.text)
            return ExtractionEnvelope(
                document_type=classification.document_type,
                classification_confidence=classification.confidence,
                classification_needs_review=classification.needs_review,
                raw_text=self.text,
                facts=extract_basic_facts(self.text),
                provider="synthetic_date_test",
                extraction_status="extracted",
            )

    texts = iter(
        (
            "ANC Record\nVisit Date: 39/19/2026",
            "ANC Record\nVisit Date: 03/04/2026",
        )
    )
    monkeypatch.setattr(
        "app.main.get_provider",
        lambda: DateFixtureProvider(next(texts)),
    )

    for index, corrected in enumerate(("2026-04-03", "2026-04-03")):
        upload = client.post(
            "/documents",
            files={
                "file": (
                    f"date-review-{index}-{uuid.uuid4().hex}.png",
                    f"synthetic date review image {index}".encode(),
                    "image/png",
                )
            },
        )
        assert upload.status_code == 200
        document_id = upload.json()["document_id"]
        field = client.get(f"/documents/{document_id}").json()["fields"][0]
        rejected = client.post(
            f"/documents/{document_id}/fields/{field['id']}/verify",
            json={
                "verified_value": field["extracted_value"],
                "verified_by": "reviewer",
            },
        )
        assert rejected.status_code == 422

        verified = client.post(
            f"/documents/{document_id}/fields/{field['id']}/verify",
            json={
                "verified_value": corrected,
                "verified_by": "reviewer",
            },
        )
        assert verified.status_code == 200


def test_external_ai_provider_requires_ocr_quote_and_human_review(monkeypatch):
    source_text = "Name: Asha Example"
    response_body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "document_type": "anc_record",
                            "classification_confidence": 0.92,
                            "language": "en",
                            "facts": [
                                {
                                    "field_name": "name",
                                    "value": "Asha Example",
                                    "confidence": 0.94,
                                    "source_text": source_text,
                                },
                                {
                                    "field_name": "diagnosis",
                                    "value": "not in source",
                                    "confidence": 0.99,
                                    "source_text": "Diagnosis: fabricated",
                                },
                                {
                                    "field_name": "visit_date",
                                    "value": "2026-01-01",
                                    "confidence": 0.99,
                                    "source_text": "Name: Asha Example",
                                },
                            ],
                        }
                    )
                }
            }
        ]
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return response_body

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 30

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, headers, json):
            assert url == "https://ai.example/v1/chat/completions"
            assert headers["Authorization"] == "Bearer test-key"
            assert json["messages"][1]["content"] == source_text
            return FakeResponse()

    class FakeOCR:
        def extract(self, _path):
            return ExtractionEnvelope(
                document_type="unknown",
                raw_text=source_text,
                provider="tesseract",
                extraction_status="extracted",
            )

    monkeypatch.setattr("app.document_intelligence.httpx.Client", FakeClient)
    provider = OpenAICompatibleDocumentProvider(
        "https://ai.example/v1",
        "test-key",
        "test-model",
        ocr_provider=FakeOCR(),
    )

    result = provider.extract("synthetic.png")

    assert result.extraction_status == "extracted"
    assert result.provider == "openai_compatible"
    assert result.document_type == "anc_record"
    assert [fact.field_name for fact in result.facts] == ["name"]
    assert result.facts[0].source_text == source_text
    assert result.facts[0].needs_review is True
    assert result.facts[0].method == "ai"
    assert result.classification_confidence == 0.92
    assert result.classification_needs_review is True
    assert any("source evidence was omitted" in warning for warning in result.warnings)


def test_external_ai_date_candidate_preserves_ambiguity_review(monkeypatch):
    source_text = "Visit Date: 03/04/2026"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "document_type": "anc_record",
                                    "classification_confidence": 0.5,
                                    "facts": [
                                        {
                                            "field_name": "visit_date",
                                            "value": "03/04/2026",
                                            "confidence": 0.9,
                                            "source_text": source_text,
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    class FakeOCR:
        def extract(self, _path):
            return ExtractionEnvelope(
                document_type="unknown",
                raw_text=source_text,
                provider="tesseract",
                extraction_status="extracted",
            )

    monkeypatch.setattr("app.document_intelligence.httpx.Client", FakeClient)
    provider = OpenAICompatibleDocumentProvider(
        "https://ai.example/v1",
        "test-key",
        "test-model",
        ocr_provider=FakeOCR(),
    )

    result = provider.extract("synthetic.png")

    assert len(result.facts) == 1
    assert result.facts[0].field_name == "visit_date"
    assert result.facts[0].value == "03/04/2026"
    assert result.facts[0].needs_review is True
    assert "ambiguous" in result.facts[0].reason.casefold()


def test_upload_persists_field_source_and_provider_provenance(client, monkeypatch):
    class FakeProvider:
        def extract(self, _path):
            return ExtractionEnvelope(
                document_type="anc_record",
                classification_confidence=0.5,
                classification_needs_review=True,
                raw_text="Name: Synthetic",
                facts=[
                    {
                        "field_name": "name",
                        "value": "Synthetic",
                        "confidence": 0.5,
                        "source_text": "Name: Synthetic",
                        "method": "rule",
                        "needs_review": True,
                    }
                ],
                warnings=["Synthetic review warning."],
                provider="synthetic_test_provider",
                model="fixture-model",
                processing_version="test-version",
                extraction_status="extracted",
            )

    monkeypatch.setattr("app.main.get_provider", lambda: FakeProvider())

    response = client.post(
        "/documents",
        files={
            "file": (
                f"{uuid.uuid4().hex}.png",
                b"synthetic image fixture",
                "image/png",
            )
        },
    )
    assert response.status_code == 200
    document_id = response.json()["document_id"]
    document = client.get(f"/documents/{document_id}").json()

    assert document["document"]["extraction_provider"] == "synthetic_test_provider"
    assert document["document"]["extraction_model"] == "fixture-model"
    assert document["document"]["processing_version"] == "test-version"
    assert document["document"]["classification_confidence"] == 0.5
    assert document["document"]["classification_needs_review"] == 1
    assert json.loads(document["document"]["extraction_warnings"]) == [
        "Synthetic review warning."
    ]
    assert document["fields"][0]["source_text"] == "Name: Synthetic"
    assert document["fields"][0]["needs_review"] == 1


def test_uncertain_classification_must_be_reviewed_before_event_creation(
    client,
    monkeypatch,
):
    person = _create_person(client, "Classification Review Person")

    class FakeProvider:
        def extract(self, _path):
            return ExtractionEnvelope(
                document_type="anc_record",
                classification_confidence=0.5,
                classification_needs_review=True,
                facts=[
                    {
                        "field_name": "anc_visit",
                        "value": 1,
                        "confidence": 0.5,
                        "source_text": "ANC Visit: 1",
                        "method": "rule",
                        "needs_review": True,
                    },
                    {
                        "field_name": "visit_date",
                        "value": "2026-05-01",
                        "confidence": 0.5,
                        "source_text": "Visit Date: 2026-05-01",
                        "method": "rule",
                        "needs_review": True,
                    },
                ],
                extraction_status="extracted",
                provider="synthetic",
            )

    monkeypatch.setattr("app.main.get_provider", lambda: FakeProvider())
    uploaded = client.post(
        "/documents",
        files={
            "file": (
                f"{uuid.uuid4().hex}.png",
                b"synthetic ANC image",
                "image/png",
            )
        },
    )
    document_id = uploaded.json()["document_id"]
    fields = client.get(f"/documents/{document_id}").json()["fields"]
    assert client.post(
        f"/documents/{document_id}/link-person",
        json={"person_id": person["id"], "linked_by": "reviewer"},
    ).status_code == 200

    last_response = None
    for field in fields:
        last_response = client.post(
            f"/documents/{document_id}/fields/{field['id']}/verify",
            json={
                "verified_value": field["extracted_value"],
                "verified_by": "reviewer",
            },
        )
    assert last_response is not None
    assert last_response.json()["document_status"] == "verified"
    assert last_response.json()["automatic_event"]["status"] == "not_ready"
    review = client.post(
        f"/people/{person['id']}/assistant",
        json={"question": "Which records require verification?"},
    )
    assert review.status_code == 200
    assert any(
        item["reason"] == "Document classification requires review."
        for item in review.json()["answer"]
    )

    premature = client.post(
        f"/documents/{document_id}/create-verified-event",
        json={
            "person_id": person["id"],
            "event_type": "anc_visit",
            "verified_by": "reviewer",
        },
    )
    assert premature.status_code == 409

    classification = client.post(
        f"/documents/{document_id}/classification/verify",
        json={"document_type": "anc_record", "verified_by": "reviewer"},
    )
    assert classification.status_code == 200
    assert classification.json()["automatic_event"]["status"] == "created"


def test_external_ai_configuration_is_opt_in_and_requires_complete_settings(monkeypatch):
    monkeypatch.setenv("ASHA_DOCUMENT_PROVIDER", "openai_compatible")
    monkeypatch.delenv("ASHA_ALLOW_EXTERNAL_AI", raising=False)

    from app.document_intelligence import configured_provider

    try:
        configured_provider()
    except ValueError as exc:
        assert "ASHA_ALLOW_EXTERNAL_AI=true" in str(exc)
    else:
        raise AssertionError("External processing must remain disabled by default.")


def test_external_ai_failure_preserves_ocr_text_without_facts(monkeypatch):
    class FakeOCR:
        def extract(self, _path):
            return ExtractionEnvelope(
                document_type="anc_record",
                raw_text="ANC visit date: 12/04/2026",
                language="eng",
                page_count=1,
                extraction_status="extracted",
            )

    class FailingClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("synthetic service failure")

    monkeypatch.setattr("app.document_intelligence.httpx.Client", FailingClient)
    provider = OpenAICompatibleDocumentProvider(
        "https://ai.example/v1",
        "test-key",
        "test-model",
        ocr_provider=FakeOCR(),
    )

    result = provider.extract("synthetic.png")

    assert result.extraction_status == "failed"
    assert result.raw_text == "ANC visit date: 12/04/2026"
    assert result.language == "eng"
    assert result.facts == []
    assert result.classification_needs_review is True
    assert "External AI extraction failed (ConnectError)." in result.warnings


def test_assistant_reports_stored_events_and_unavailable_workflows(client):
    person = _create_person(client, "Synthetic Person")
    response = client.post(
        "/events",
        json={
            "external_id": f"AI-EV-{uuid.uuid4().hex}",
            "person_id": person["id"],
            "event_type": "home_visit",
            "event_date": "2026-09-01",
            "details": {"note": "synthetic fixture"},
            "verification_status": "unverified",
        },
    )
    assert response.status_code == 200

    events = client.post(
        f"/people/{person['id']}/assistant",
        json={"question": "What recorded events are there?"},
    )
    assert events.status_code == 200
    payload = events.json()
    assert payload["engine"] == "structured_database_retrieval"
    assert payload["model"] is None
    assert payload["answer"][0]["verification_status"] == "unverified"
    assert payload["sources"][0]["id"] == response.json()["id"]

    follow_up = client.post(
        f"/people/{person['id']}/assistant",
        json={"question": "Which follow-up tasks are pending?"},
    )
    assert follow_up.json()["status"] == "unavailable"
    assert "no follow-up task records" in follow_up.json()["reason"]

    missing = client.post(
        f"/people/{person['id']}/assistant",
        json={"question": "What information is missing from the record?"},
    )
    assert missing.json()["status"] == "unavailable"
    assert not missing.json()["sources"]


def test_longitudinal_changes_compare_only_verified_linked_sources(client):
    person = _create_person(client, "Longitudinal Person")
    document_ids = []
    event_ids = []
    token = uuid.uuid4().hex
    with connection() as conn:
        for index, (visit_date, weight) in enumerate(
            (("2026-01-10", "50"), ("2026-03-10", "52")),
            start=1,
        ):
            cur = conn.execute(
                """INSERT INTO documents (
                       external_id, storage_path, document_type, extraction_status,
                       verification_status, created_by
                   ) VALUES (?, ?, 'anc_record', 'extracted', 'verified', 'development')""",
                (f"AI-DOC-{token}-{index}", f"synthetic/{token}-{index}.png"),
            )
            document_id = cur.lastrowid
            document_ids.append(document_id)
            conn.execute(
                """INSERT INTO document_person_links (
                       document_id, person_id, linked_by
                   ) VALUES (?, ?, 'fixture')""",
                (document_id, person["id"]),
            )
            for field_name, value in (
                ("visit_date", visit_date),
                ("weight_kg", weight),
            ):
                conn.execute(
                    """INSERT INTO document_fields (
                           document_id, field_name, extracted_value, verified_value,
                           confidence, extraction_method, needs_review
                       ) VALUES (?, ?, ?, ?, 1.0, 'manual', 0)""",
                    (document_id, field_name, value, value),
                )
            event = conn.execute(
                """INSERT INTO events (
                       external_id, person_id, event_type, event_date, details_json,
                       source_document_id, verification_status, verified_by, verified_at
                   ) VALUES (?, ?, 'anc_visit', ?, '{}', ?, 'verified', 'reviewer', CURRENT_TIMESTAMP)""",
                (
                    f"AI-EVENT-{token}-{index}",
                    person["id"],
                    visit_date,
                    document_id,
                ),
            )
            event_ids.append(event.lastrowid)

    response = client.post(
        f"/people/{person['id']}/assistant",
        json={"question": "What changed between the previous and latest documented visits?"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "available"
    assert payload["visits_compared"] == [
        {
            "event_id": event_ids[0],
            "event_date": "2026-01-10",
            "document_id": document_ids[0],
        },
        {
            "event_id": event_ids[1],
            "event_date": "2026-03-10",
            "document_id": document_ids[1],
        },
    ]
    assert len(payload["answer"]) == 1
    change = payload["answer"][0]
    assert change["field_name"] == "weight_kg"
    assert change["previous_value"] == "50"
    assert change["latest_value"] == "52"
    assert [source["document_id"] for source in change["sources"]] == document_ids
    assert [source["event_id"] for source in change["sources"]] == event_ids
    assert all(source["type"] == "document_field" for source in change["sources"])

    summary = client.get(f"/people/{person['id']}/health-summary")
    assert summary.status_code == 200
    assert [record["record_type"] for record in summary.json()["chronological_records"]] == [
        "document",
        "event",
        "document",
        "event",
    ]


def test_assistant_service_rejects_person_outside_supplied_scope():
    principal = Principal(subject="limited", role="asha", person_ids=frozenset({9999}))
    with connection() as conn:
        try:
            answer_question(conn, principal, 10000, "What recorded events are there?")
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
        else:
            raise AssertionError("The retrieval service must enforce its person scope.")


def test_ai_router_is_registered():
    with TestClient(app) as test_client:
        paths = test_client.get("/openapi.json").json()["paths"]
    assert "/people/{person_id}/assistant" in paths
