"""Run a synthetic, upload-API-level evaluation of the current rule pipeline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, PngImagePlugin

from app import db, main
from app.classification import classify_document
from app.extraction import extract_basic_facts
from app.schema import ExtractionEnvelope


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "tests" / "fixtures" / "document_intelligence_eval.json"


def _load_dataset() -> dict:
    with DATASET_PATH.open(encoding="utf-8") as dataset_file:
        return json.load(dataset_file)


def _make_synthetic_png(case: dict) -> bytes:
    image = Image.new("RGB", (900, 300), "white")
    draw = ImageDraw.Draw(image)
    lines = case["ocr_text"].splitlines() or ["Unreadable synthetic scan"]
    for index, line in enumerate(lines):
        draw.text((20, 20 + index * 30), line, fill="black")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("evaluation_case_id", case["case_id"])
    from io import BytesIO

    image_bytes = BytesIO()
    image.save(image_bytes, format="PNG", pnginfo=metadata)
    return image_bytes.getvalue()


class SyntheticEvaluationProvider:
    """Fixture-only OCR adapter; it never calls Tesseract or a model provider."""

    name = "synthetic_evaluation_fixture"
    model = None

    def __init__(self, cases: list[dict]):
        self.cases = {case["case_id"]: case for case in cases}
        self.calls: dict[str, int] = defaultdict(int)

    def extract(self, path: str) -> ExtractionEnvelope:
        with Image.open(path) as image:
            case_id = image.info["evaluation_case_id"]
        case = self.cases[case_id]
        self.calls[case_id] += 1

        if case["ocr_status"] == "failed":
            return ExtractionEnvelope(
                document_type="unknown",
                classification_needs_review=True,
                provider=self.name,
                warnings=["Synthetic fixture marked OCR as unreadable; no OCR ran."],
                extraction_status="failed",
            )

        text = case["ocr_text"]
        classification = classify_document(text)
        warnings = (
            ["Synthetic fixture OCR confidence is below 60%."]
            if case["ocr_confidence"] < 60
            else []
        )
        if classification.ambiguous and classification.reason:
            warnings.append(classification.reason)
        return ExtractionEnvelope(
            document_type=classification.document_type,
            classification_confidence=classification.confidence,
            classification_needs_review=classification.needs_review,
            language="en",
            raw_text=text,
            facts=extract_basic_facts(text),
            warnings=warnings,
            extraction_status="extracted",
            provider=self.name,
            processing_version="synthetic-evaluation-v1",
        )


def _normalized(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _field_metrics(
    expected: dict[str, str], predicted: dict[str, str]
) -> dict:
    expected_names = set(expected)
    predicted_names = set(predicted)
    name_tp = len(expected_names & predicted_names)
    name_fp = len(predicted_names - expected_names)
    name_fn = len(expected_names - predicted_names)
    name_precision = _ratio(name_tp, name_tp + name_fp)
    name_recall = _ratio(name_tp, name_tp + name_fn)
    f1 = (
        2 * name_precision * name_recall / (name_precision + name_recall)
        if name_precision is not None
        and name_recall is not None
        and name_precision + name_recall
        else 0.0
        if name_precision is not None and name_recall is not None
        else None
    )
    exact_pairs = sum(
        predicted.get(name) == value for name, value in expected.items()
    )
    exact_fp = sum(
        (name, value) not in expected.items()
        for name, value in predicted.items()
    )
    exact_fn = sum(
        predicted.get(name) != value for name, value in expected.items()
    )
    return {
        "field_name_precision": name_precision,
        "field_name_recall": name_recall,
        "field_name_f1": f1,
        "exact_field_value_accuracy": _ratio(exact_pairs, len(expected)),
        "exact_field_value_precision": _ratio(
            exact_pairs, exact_pairs + exact_fp
        ),
        "exact_field_value_recall": _ratio(
            exact_pairs, exact_pairs + exact_fn
        ),
        "expected_field_count": len(expected),
        "predicted_field_count": len(predicted),
    }


def run_evaluation(client: TestClient) -> dict:
    """Upload each synthetic fixture through POST /documents and score persisted output."""
    dataset = _load_dataset()
    cases = dataset["documents"]
    provider = SyntheticEvaluationProvider(cases)
    with patch.object(main, "get_provider", return_value=provider):
        evaluated = []
        evaluation_person_id = None
        for case in cases:
            image_bytes = _make_synthetic_png(case)
            response = client.post(
                "/documents",
                files={
                    "file": (
                        f'{case["case_id"]}.png',
                        image_bytes,
                        "image/png",
                    )
                },
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f'{case["case_id"]} upload failed with HTTP '
                    f"{response.status_code}: {response.text}"
                )
            upload = response.json()
            link_conflicts = []
            if case["conflict_group"]:
                if evaluation_person_id is None:
                    household = client.post(
                        "/households",
                        json={
                            "external_id": "EVAL-HH-SYNTHETIC",
                            "village_id": "Synthetic evaluation village",
                        },
                    ).json()
                    person = client.post(
                        "/people",
                        json={
                            "external_id": "EVAL-P-SYNTHETIC",
                            "household_id": household["id"],
                            "name": "Synthetic Evaluation Person",
                        },
                    ).json()
                    evaluation_person_id = person["id"]
                linked = client.post(
                    f'/documents/{upload["document_id"]}/link-person',
                    json={
                        "person_id": evaluation_person_id,
                        "linked_by": "synthetic-evaluation",
                    },
                )
                if linked.status_code != 200:
                    raise RuntimeError(
                        f'{case["case_id"]} link failed with HTTP '
                        f"{linked.status_code}: {linked.text}"
                    )
                link_conflicts = linked.json()["conflicts"]
            persisted = client.get(
                f'/documents/{upload["document_id"]}'
            ).json()
            document = persisted["document"]
            fields = persisted["fields"]
            predicted_fields = {}
            for field in fields:
                if (
                    field["field_name"] in {"visit_date", "event_date"}
                    and "invalid calendar date"
                    in (field["review_reason"] or "").casefold()
                ):
                    continue
                predicted_fields[field["field_name"]] = field["extracted_value"]
            review_required = (
                document["extraction_status"] == "failed"
                or bool(document["classification_needs_review"])
                or any(bool(field["needs_review"]) for field in fields)
            )
            evidence_ok = {
                field["field_name"]: bool(
                    _normalized(field["source_text"])
                    and _normalized(field["source_text"])
                    in _normalized(document["ocr_text"])
                )
                for field in fields
            }
            evaluated.append(
                {
                    "case": case,
                    "document_id": upload["document_id"],
                    "document": document,
                    "fields": fields,
                    "predicted_fields": predicted_fields,
                    "review_required": review_required,
                    "evidence_ok": evidence_ok,
                    "link_conflicts": link_conflicts,
                }
            )

            if case["duplicate_replay"]:
                replay = client.post(
                    "/documents",
                    files={
                        "file": (
                            f'{case["case_id"]}-duplicate.png',
                            image_bytes,
                            "image/png",
                        )
                    },
                )
                if replay.status_code != 200 or replay.json()["status"] != "duplicate":
                    raise AssertionError(
                        f'{case["case_id"]} duplicate replay was not safely '
                        f"deduplicated: HTTP {replay.status_code}"
                    )

        unsupported = dataset["unsupported_upload"]
        unsupported_response = client.post(
            "/documents",
            files={
                "file": (
                    unsupported["filename"],
                    unsupported["content"].encode(),
                    "application/pdf",
                )
            },
        )
        unsupported_ok = (
            unsupported_response.status_code
            == unsupported["expected_http_status"]
        )

    eligible = [
        item
        for item in evaluated
        if item["document"]["extraction_status"] == "extracted"
    ]
    correct_classifications = sum(
        item["document"]["document_type"]
        == item["case"]["expected_document_type"]
        for item in eligible
    )
    expected_pairs = [
        (item["case"]["case_id"], name, value)
        for item in evaluated
        for name, value in item["case"]["expected_fields"].items()
    ]
    predicted_pairs = [
        (item["case"]["case_id"], name, value)
        for item in evaluated
        for name, value in item["predicted_fields"].items()
    ]
    expected_pair_set = set(expected_pairs)
    predicted_pair_set = set(predicted_pairs)
    exact_pair_tp = len(expected_pair_set & predicted_pair_set)
    exact_pair_fp = len(predicted_pair_set - expected_pair_set)
    exact_pair_fn = len(expected_pair_set - predicted_pair_set)
    exact_pair_precision = _ratio(
        exact_pair_tp, exact_pair_tp + exact_pair_fp
    )
    exact_pair_recall = _ratio(
        exact_pair_tp, exact_pair_tp + exact_pair_fn
    )
    exact_pair_f1 = (
        2 * exact_pair_precision * exact_pair_recall
        / (exact_pair_precision + exact_pair_recall)
        if exact_pair_precision is not None
        and exact_pair_recall is not None
        and exact_pair_precision + exact_pair_recall
        else 0.0
        if exact_pair_precision is not None and exact_pair_recall is not None
        else None
    )

    date_cases = [
        item
        for item in evaluated
        if item["case"]["category"] == "ambiguous_date"
        or item["case"]["category"] == "invalid_date"
        or "visit_date" in item["case"]["expected_fields"]
    ]
    date_results = []
    for item in date_cases:
        case = item["case"]
        date_fact = next(
            (
                field
                for field in item["fields"]
                if field["field_name"] == "visit_date"
            ),
            None,
        )
        reason = (
            (date_fact["review_reason"] or "").casefold()
            if date_fact
            else ""
        )
        if case.get("invalid_dates"):
            correct = (
                date_fact is not None
                and "invalid calendar date" in reason
                and date_fact["extracted_value"] in case["invalid_dates"]
                and "visit_date" not in item["predicted_fields"]
            )
        elif "date_ambiguity" in case:
            correct = (
                date_fact is not None
                and date_fact["extracted_value"]
                == case["expected_fields"].get("visit_date")
                and "ambiguous" in reason
            )
        else:
            correct = (
                item["predicted_fields"].get("visit_date")
                == case["expected_fields"].get("visit_date")
                and "ambiguous" not in reason
            )
        date_results.append(correct)
    exact_dates = sum(date_results)
    expected_review_cases = [
        item for item in evaluated if item["case"]["expected_review"] is not None
    ]
    correct_review = sum(
        item["review_required"] == item["case"]["expected_review"]
        for item in expected_review_cases
    )
    predicted_facts = [field for item in evaluated for field in item["fields"]]
    grounded_facts = sum(
        item["evidence_ok"].get(field["field_name"], False)
        for item in evaluated
        for field in item["fields"]
    )
    expected_evidence_pairs = {
        (
            item["case"]["case_id"],
            name,
            _normalized(source_text),
        )
        for item in evaluated
        for name, source_text in item["case"]["expected_evidence"].items()
    }
    predicted_evidence_pairs = {
        (
            item["case"]["case_id"],
            field["field_name"],
            _normalized(field["source_text"]),
        )
        for item in evaluated
        for field in item["fields"]
        if field["source_text"]
    }
    evidence_tp = len(expected_evidence_pairs & predicted_evidence_pairs)
    evidence_fp = len(predicted_evidence_pairs - expected_evidence_pairs)
    evidence_fn = len(expected_evidence_pairs - predicted_evidence_pairs)
    evidence_precision = _ratio(evidence_tp, evidence_tp + evidence_fp)
    evidence_recall = _ratio(evidence_tp, evidence_tp + evidence_fn)
    evidence_f1 = (
        2 * evidence_precision * evidence_recall
        / (evidence_precision + evidence_recall)
        if evidence_precision is not None
        and evidence_recall is not None
        and evidence_precision + evidence_recall
        else 0.0
        if evidence_precision is not None and evidence_recall is not None
        else None
    )

    by_category = {}
    for category in sorted({item["case"]["category"] for item in evaluated}):
        group = [item for item in evaluated if item["case"]["category"] == category]
        group_eligible = [
            item
            for item in group
            if item["document"]["extraction_status"] == "extracted"
        ]
        correct = sum(
            item["document"]["document_type"]
            == item["case"]["expected_document_type"]
            for item in group_eligible
        )
        by_category[category] = {
            "documents": len(group),
            "classification": {
                "correct": correct,
                "denominator": len(group_eligible),
                "accuracy": _ratio(correct, len(group_eligible)),
            },
            "fields": _field_metrics(
                {
                    f'{item["case"]["case_id"]}:{name}': value
                    for item in group
                    for name, value in item["case"]["expected_fields"].items()
                },
                {
                    f'{item["case"]["case_id"]}:{name}': value
                    for item in group
                    for name, value in item["predicted_fields"].items()
                },
            ),
        }

    conflict_groups: dict[str, list[dict]] = defaultdict(list)
    for item in evaluated:
        if item["case"]["conflict_group"]:
            conflict_groups[item["case"]["conflict_group"]].append(item)
    conflict_results = []
    for group_name, group in sorted(conflict_groups.items()):
        flagged = any(item["link_conflicts"] for item in group)
        conflict_results.append(
            {
                "group": group_name,
                "expected_conflict": True,
                "explicitly_flagged_for_conflict_review": flagged,
            }
        )

    failures = []
    for item in evaluated:
        case = item["case"]
        doc = item["document"]
        for name, expected in case["expected_fields"].items():
            actual = item["predicted_fields"].get(name)
            if actual != expected:
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "category": case["category"],
                        "issue": "field_value_mismatch",
                        "field": name,
                        "expected": expected,
                        "actual": actual,
                    }
                )
        for name, actual in item["predicted_fields"].items():
            if name not in case["expected_fields"]:
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "category": case["category"],
                        "issue": "unexpected_field",
                        "field": name,
                        "actual": actual,
                    }
                )
        if doc["document_type"] != case["expected_document_type"]:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "issue": "classification_mismatch",
                    "expected": case["expected_document_type"],
                    "actual": doc["document_type"],
                }
            )
        if not item["review_required"] and case["expected_review"]:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "issue": "human_review_not_required",
                }
            )
        for field in item["fields"]:
            if not item["evidence_ok"].get(field["field_name"], False):
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "category": case["category"],
                        "issue": "source_evidence_not_grounded",
                        "field": field["field_name"],
                        "source_text": field["source_text"],
                    }
                )
        for name, expected_quote in case["expected_evidence"].items():
            actual_quote = next(
                (
                    field["source_text"]
                    for field in item["fields"]
                    if field["field_name"] == name
                ),
                None,
            )
            if _normalized(actual_quote) != _normalized(expected_quote):
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "category": case["category"],
                        "issue": "expected_source_quote_mismatch",
                        "field": name,
                        "expected": expected_quote,
                        "actual": actual_quote,
                    }
                )
        if "date_ambiguity" in case and not any(
            "ambig" in (field["review_reason"] or "").casefold()
            or "ambig" in (field["source_text"] or "").casefold()
            for field in item["fields"]
            if field["field_name"] == "visit_date"
        ):
            failures.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "issue": "date_ambiguity_not_explicitly_identified",
                }
            )
    for conflict in conflict_results:
        if not conflict["explicitly_flagged_for_conflict_review"]:
            failures.append(
                {
                    "case_id": conflict["group"],
                    "category": "conflicting_values",
                    "issue": "cross_document_conflict_not_explicitly_flagged",
                }
            )
    if not unsupported_ok:
        failures.append(
            {
                "case_id": "unsupported_upload",
                "category": "unsupported_document",
                "issue": "unsupported_upload_not_rejected",
                "http_status": unsupported_response.status_code,
            }
        )

    return {
        "dataset_id": dataset["dataset_id"],
        "dataset_privacy": dataset["privacy"],
        "execution": {
            "pipeline": "POST /documents upload endpoint and persisted GET /documents/{id} output",
            "provider": provider.name,
            "ocr": "synthetic transcript fixture; no OCR software executed",
            "model": "none; no model request made",
            "uploaded_documents": len(evaluated),
            "eligible_for_classification": len(eligible),
            "duplicate_replays": sum(
                case["duplicate_replay"] for case in cases
            ),
            "provider_calls": dict(provider.calls),
        },
        "metrics": {
            "classification": {
                "correct": correct_classifications,
                "denominator": len(eligible),
                "accuracy": _ratio(correct_classifications, len(eligible)),
                "denominator_method": (
                    "Uploaded fixtures with extraction_status=extracted; "
                    "unreadable OCR failures excluded, unknown remains a valid class."
                ),
            },
            "invalid_date_rejection": {
                "invalid_date_cases": sum(
                    bool(item["case"].get("invalid_dates"))
                    for item in evaluated
                ),
                "rejected_as_valid_dates": sum(
                    bool(item["case"].get("invalid_dates"))
                    and "visit_date" not in item["predicted_fields"]
                    and any(
                        field["field_name"] == "visit_date"
                        and "invalid calendar date"
                        in (field["review_reason"] or "").casefold()
                        for field in item["fields"]
                    )
                    for item in evaluated
                ),
            },
            "field_level": {
                "exact_field_value_precision": _ratio(
                    exact_pair_tp, exact_pair_tp + exact_pair_fp
                ),
                "exact_field_value_recall": _ratio(
                    exact_pair_tp, exact_pair_tp + exact_pair_fn
                ),
                "exact_field_value_f1": exact_pair_f1,
                "true_positive": exact_pair_tp,
                "false_positive": exact_pair_fp,
                "false_negative": exact_pair_fn,
                "denominator_method": (
                    "Micro-averaged exact (case_id, field_name, value) pairs; "
                    "wrong values count as one false positive and one false negative."
                ),
            },
            "dates": {
                "exact_decisions_correct": exact_dates,
                "denominator": len(date_cases),
                "exact_decision_accuracy": _ratio(exact_dates, len(date_cases)),
                "denominator_method": (
                    "Cases explicitly testing a visit date: exact string must match, "
                    "or no date may be emitted when ground truth expects rejection."
                ),
            },
            "evidence_grounding": {
                "grounded_facts": grounded_facts,
                "predicted_facts": len(predicted_facts),
                "accuracy": _ratio(grounded_facts, len(predicted_facts)),
                "denominator_method": (
                    "Persisted facts whose non-empty source_text is a normalized "
                    "substring of the persisted OCR text."
                ),
            },
            "human_review_routing": {
                "correct": correct_review,
                "denominator": len(expected_review_cases),
                "accuracy": _ratio(correct_review, len(expected_review_cases)),
                "denominator_method": (
                    "All labeled synthetic uploads; review is indicated by failed "
                    "extraction, classification_needs_review, or any needs_review fact."
                ),
            },
            "ambiguous_date_review": {
                "cases": sum(
                    "date_ambiguity" in item["case"] for item in evaluated
                ),
                "ambiguities_explicitly_identified": sum(
                    "date_ambiguity" in item["case"]
                    and any(
                        "ambig" in (
                            (field["review_reason"] or "").casefold()
                        )
                        or "ambig" in (field["source_text"] or "").casefold()
                        for field in item["fields"]
                        if field["field_name"] == "visit_date"
                    )
                    for item in evaluated
                ),
                "interpretation": (
                    "A generic review-required flag is not counted as identifying "
                    "the specific date ambiguity."
                ),
            },
            "low_ocr_confidence_flagging": {
                "low_confidence_cases": sum(
                    item["case"]["ocr_confidence"] is not None
                    and item["case"]["ocr_confidence"] < 60
                    for item in evaluated
                ),
                "cases_with_low_confidence_warning": sum(
                    item["case"]["ocr_confidence"] is not None
                    and item["case"]["ocr_confidence"] < 60
                    and any(
                        "below 60%" in warning
                        for warning in json.loads(
                            item["document"]["extraction_warnings"] or "[]"
                        )
                    )
                    for item in evaluated
                ),
                "interpretation": (
                    "Uses fixture-provided confidence metadata and warning; no "
                    "image-derived OCR confidence was measured."
                ),
            },
            "source_evidence_exactness": {
                "precision": evidence_precision,
                "recall": evidence_recall,
                "f1": evidence_f1,
                "true_positive": evidence_tp,
                "false_positive": evidence_fp,
                "false_negative": evidence_fn,
                "denominator_method": (
                    "Exact normalized (case_id, field_name, source_text) pairs "
                    "against each fixture's explicitly labeled expected quote."
                ),
            },
            "unsupported_upload": {
                "correct_rejections": int(unsupported_ok),
                "denominator": 1,
                "http_status": unsupported_response.status_code,
            },
            "ocr_failures": {
                "fixture_failures": sum(
                    item["document"]["extraction_status"] == "failed"
                    for item in evaluated
                ),
                "uploaded_documents": len(evaluated),
                "failure_rate": _ratio(
                    sum(
                        item["document"]["extraction_status"] == "failed"
                        for item in evaluated
                    ),
                    len(evaluated),
                ),
                "interpretation": "Injected fixture failure count, not a real OCR failure rate.",
            },
            "cross_document_conflicts": {
                "explicitly_flagged": sum(
                    result["explicitly_flagged_for_conflict_review"]
                    for result in conflict_results
                ),
                "conflict_groups": len(conflict_results),
                "results": conflict_results,
            },
            "duplicate_processing": {
                "replay_attempts": sum(case["duplicate_replay"] for case in cases),
                "replay_calls_processed_again": sum(
                    provider.calls[case["case_id"]] > 1
                    for case in cases
                    if case["duplicate_replay"]
                ),
            },
            "by_category": by_category,
        },
        "failures": failures,
        "limitations": [
            "Synthetic, curated fixtures are not representative prevalence samples.",
            "OCR output is injected from fixture transcripts; image recognition was not evaluated.",
            "No live model provider or external service was configured or called.",
            "The rule classifier's uncalibrated scores are not confidence probabilities.",
            "Review metrics measure persisted review flags only; this pipeline does "
            "not assign cases to an operational human-review queue.",
            "Generic review flags do not establish that date ambiguity or cross-document "
            "conflict was recognized.",
        ],
    }


def _run_isolated_evaluation() -> dict:
    with tempfile.TemporaryDirectory(prefix="asha-document-eval-") as directory:
        root = Path(directory)
        document_dir = root / "documents"
        document_dir.mkdir()
        with (
            patch.dict(os.environ, {"ASHA_ENV": "development"}),
            patch.object(db, "DB_PATH", root / "evaluation.db"),
            patch.object(main, "DOCUMENT_DIR", document_dir),
        ):
            db.init_db()
            with TestClient(main.app) as client:
                return run_evaluation(client)


def main_cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path; defaults to stdout.",
    )
    arguments = parser.parse_args()
    result = _run_isolated_evaluation()
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main_cli()
