import uuid
from pathlib import Path

from scripts.evaluate_document_intelligence import run_evaluation
from app.schema import ExtractionEnvelope


def test_synthetic_evaluation_reports_pipeline_metrics_and_failures(client):
    report = run_evaluation(client)
    metrics = report["metrics"]

    assert report["dataset_id"] == "asha-synthetic-document-evaluation-v1"
    assert report["execution"]["pipeline"].startswith("POST /documents")
    assert report["execution"]["ocr"] == (
        "synthetic transcript fixture; no OCR software executed"
    )
    assert report["execution"]["model"] == "none; no model request made"
    assert report["execution"]["uploaded_documents"] == 9
    assert report["execution"]["duplicate_replays"] == 1
    assert metrics["classification"]["correct"] == 5
    assert metrics["classification"]["denominator"] == 8
    assert metrics["classification"]["accuracy"] == 0.625
    assert metrics["field_level"]["true_positive"] == 18
    assert metrics["field_level"]["false_positive"] == 1
    assert metrics["field_level"]["false_negative"] == 0
    assert round(metrics["field_level"]["exact_field_value_f1"], 3) == 0.973
    assert metrics["dates"]["exact_decisions_correct"] == 6
    assert metrics["dates"]["denominator"] == 7
    assert metrics["evidence_grounding"]["grounded_facts"] == 19
    assert metrics["evidence_grounding"]["predicted_facts"] == 19
    assert metrics["source_evidence_exactness"]["true_positive"] == 18
    assert metrics["source_evidence_exactness"]["false_positive"] == 1
    assert metrics["human_review_routing"]["correct"] == 9
    assert metrics["unsupported_upload"]["http_status"] == 415
    assert metrics["cross_document_conflicts"]["explicitly_flagged"] == 0
    assert metrics["duplicate_processing"]["replay_calls_processed_again"] == 0
    assert metrics["ambiguous_date_review"]["ambiguities_explicitly_identified"] == 0
    assert metrics["low_ocr_confidence_flagging"]["low_confidence_cases"] == 1
    assert (
        metrics["low_ocr_confidence_flagging"]["cases_with_low_confidence_warning"]
        == 1
    )
    assert any(
        failure["issue"] == "date_ambiguity_not_explicitly_identified"
        for failure in report["failures"]
    )
    assert any(
        failure["issue"] == "unexpected_field"
        and failure["field"] == "visit_date"
        for failure in report["failures"]
    )


def test_upload_rejects_missing_and_unsupported_document_inputs(client):
    missing_file = client.post("/documents")
    unsupported = client.post(
        "/documents",
        files={"file": ("not-supported.pdf", b"synthetic", "application/pdf")},
    )

    assert missing_file.status_code == 422
    assert unsupported.status_code == 415


def test_upload_persists_ocr_failure_without_fabricated_facts(client, monkeypatch):
    class FailedOCRProvider:
        def extract(self, path):
            assert Path(path).is_file()
            return ExtractionEnvelope(
                document_type="unknown",
                classification_needs_review=True,
                provider="synthetic_failure",
                warnings=["Synthetic OCR failure."],
                extraction_status="failed",
            )

    monkeypatch.setattr(
        "app.main.get_provider", lambda: FailedOCRProvider()
    )
    uploaded = client.post(
        "/documents",
        files={
            "file": (
                f"{uuid.uuid4().hex}.png",
                b"unique synthetic image",
                "image/png",
            )
        },
    )
    assert uploaded.status_code == 200

    persisted = client.get(
        f'/documents/{uploaded.json()["document_id"]}'
    ).json()
    assert persisted["document"]["extraction_status"] == "failed"
    assert persisted["document"]["ocr_text"] == ""
    assert persisted["fields"] == []
