"""Generate synthetic scans and evaluate them through the real Tesseract upload path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytesseract
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app import db, main
from app.classification import DOCUMENT_TYPES
from app.document_intelligence import configured_provider
from app.ocr.tesseract_provider import TesseractOCRProvider


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "tests" / "fixtures" / "real_ocr_scan_cases.json"
DEFAULT_FONTS = (
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\calibri.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
)
DEFAULT_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
FIELD_LABELS = {
    "anc_visit": "ANC Visit:",
    "visit_date": "Visit Date:",
    "event_date": "Event Date:",
    "blood_pressure": "Blood Pressure:",
    "weight_kg": "Weight:",
    "hemoglobin": "Hemoglobin:",
}


def load_dataset() -> dict:
    with DATASET_PATH.open(encoding="utf-8") as dataset_file:
        return json.load(dataset_file)


def select_font(font_path: Path | None = None) -> Path:
    candidates = (font_path,) if font_path else DEFAULT_FONTS
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No supported Windows font was found. Supply --font with the absolute "
        "path to an installed TrueType font."
    )


def generate_scan(case: dict, font_path: Path) -> tuple[bytes, str, str]:
    """Generate one deterministic synthetic scan from its labeled case."""
    width = int(case["width"])
    height = int(case["height"])
    image = Image.new("RGB", (width, height), "white")

    if not case.get("unreadable"):
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(font_path), int(case["font_size"]))
        text_color = int(case.get("text_color", 0))
        y = max(28, height // 12)
        line_gap = int(case["font_size"] * 1.8)
        for line in case["lines"]:
            draw.text((width // 14, y), line, fill=(text_color,) * 3, font=font)
            y += line_gap
    else:
        draw = ImageDraw.Draw(image)
        for x, y, size in (
            (width // 5, height // 4, 2),
            (width // 2, height // 2, 1),
            (width * 3 // 4, height * 2 // 3, 2),
        ):
            draw.ellipse((x, y, x + size, y + size), fill=(215, 215, 215))

    rotation = float(case.get("rotation_degrees", 0))
    if rotation:
        image = image.rotate(
            rotation,
            resample=Image.Resampling.BICUBIC,
            expand=False,
            fillcolor="white",
        )
    blur_radius = float(case.get("blur_radius", 0))
    if blur_radius:
        image = image.filter(ImageFilter.GaussianBlur(blur_radius))

    jpeg_quality = case.get("jpeg_quality")
    if jpeg_quality is not None:
        buffer = BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=int(jpeg_quality),
            optimize=False,
            progressive=False,
        )
        return buffer.getvalue(), ".jpg", "image/jpeg"

    buffer = BytesIO()
    image.save(buffer, format="PNG", compress_level=9)
    return buffer.getvalue(), ".png", "image/png"


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _require_tesseract(
    executable: Path | None = None,
) -> tuple[TesseractOCRProvider, str, str]:
    candidates = (
        (executable,)
        if executable is not None
        else (
            Path(shutil.which("tesseract")) if shutil.which("tesseract") else None,
            DEFAULT_TESSERACT,
        )
    )
    selected_executable = next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )
    if selected_executable is None:
        raise RuntimeError(
            "Real OCR evaluation is blocked: Tesseract was not found on PATH or "
            "at the standard Windows installation path. Install it only after "
            "operator approval, then provide --tesseract-executable or verify "
            "with `tesseract --version`."
        )
    pytesseract.pytesseract.tesseract_cmd = str(selected_executable)
    try:
        version = str(pytesseract.get_tesseract_version())
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError(
            "Tesseract was found but Python could not execute it. Check the "
            "executable's permissions and installation."
        ) from exc

    provider = configured_provider()
    if not isinstance(provider, TesseractOCRProvider):
        raise RuntimeError(
            "Real OCR evaluation requires ASHA_DOCUMENT_PROVIDER=tesseract; "
            f"configured provider was {provider.name!r}."
        )
    return provider, version, str(selected_executable)


def _expected_quote(case: dict, field_name: str) -> str | None:
    prefix = FIELD_LABELS.get(field_name)
    if prefix is None:
        return None
    return next(
        (line for line in case["lines"] if line.casefold().startswith(prefix.casefold())),
        None,
    )


def _create_person(client: TestClient, group: str) -> int:
    household = client.post(
        "/households",
        json={
            "external_id": f"REAL-OCR-HH-{group}",
            "village_id": "Synthetic Evaluation",
        },
    )
    if household.status_code != 200:
        raise RuntimeError(
            f"Synthetic evaluation household creation failed: {household.text}"
        )
    person = client.post(
        "/people",
        json={
            "external_id": f"REAL-OCR-P-{group}",
            "household_id": household.json()["id"],
            "name": f"Synthetic {group}",
        },
    )
    if person.status_code != 200:
        raise RuntimeError(
            f"Synthetic evaluation person creation failed: {person.text}"
        )
    return person.json()["id"]


def run_real_ocr_evaluation(
    client: TestClient,
    font_path: Path,
    tesseract_executable: Path | None = None,
) -> dict:
    dataset = load_dataset()
    cases = dataset["cases"]
    provider, tesseract_version, executable_path = _require_tesseract(
        tesseract_executable
    )
    person_ids: dict[str, int] = {}
    results: list[dict] = []

    with patch.object(main, "get_provider", return_value=provider):
        for case in cases:
            image_bytes, suffix, content_type = generate_scan(case, font_path)
            response = client.post(
                "/documents",
                files={
                    "file": (
                        f'{case["case_id"]}{suffix}',
                        image_bytes,
                        content_type,
                    )
                },
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f'{case["case_id"]} upload failed with HTTP '
                    f"{response.status_code}: {response.text}"
                )
            document_id = response.json()["document_id"]
            link_conflicts = []
            group = case.get("person_group")
            if group:
                if group not in person_ids:
                    person_ids[group] = _create_person(client, group)
                linked = client.post(
                    f"/documents/{document_id}/link-person",
                    json={
                        "person_id": person_ids[group],
                        "linked_by": "real-ocr-synthetic-evaluation",
                    },
                )
                if linked.status_code != 200:
                    raise RuntimeError(
                        f'{case["case_id"]} person link failed with HTTP '
                        f"{linked.status_code}: {linked.text}"
                    )
                link_conflicts = linked.json()["conflicts"]

            persisted_response = client.get(f"/documents/{document_id}")
            if persisted_response.status_code != 200:
                raise RuntimeError(
                    f'{case["case_id"]} persisted document could not be read: '
                    f"{persisted_response.text}"
                )
            persisted = persisted_response.json()
            document = persisted["document"]
            fields = persisted["fields"]
            actual_fields = {
                field["field_name"]: field for field in fields
            }
            accepted_fields = {
                name: field["extracted_value"]
                for name, field in actual_fields.items()
                if not (
                    name in {"visit_date", "event_date"}
                    and "invalid calendar date"
                    in (field["review_reason"] or "").casefold()
                )
            }
            results.append(
                {
                    "case": case,
                    "document_id": document_id,
                    "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                    "image_extension": suffix,
                    "document": document,
                    "fields": fields,
                    "actual_fields": actual_fields,
                    "accepted_fields": accepted_fields,
                    "link_conflicts": link_conflicts,
                }
            )

    unsupported = dataset["unsupported_document"]
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
        unsupported_response.status_code == unsupported["expected_http_status"]
    )

    eligible = [
        item
        for item in results
        if item["document"]["extraction_status"] == "extracted"
    ]
    predicted_types = [
        item["document"]["document_type"]
        if item["document"]["document_type"] in DOCUMENT_TYPES
        else "unknown"
        for item in eligible
    ]
    expected_types = [item["case"]["expected_document_type"] for item in eligible]
    labels = sorted(DOCUMENT_TYPES)
    confusion = {label: {predicted: 0 for predicted in labels} for label in labels}
    for expected, predicted in zip(expected_types, predicted_types, strict=True):
        confusion[expected][predicted] += 1
    correct_classifications = sum(
        expected == predicted
        for expected, predicted in zip(expected_types, predicted_types, strict=True)
    )

    expected_pairs = {
        (item["case"]["case_id"], name, value)
        for item in results
        for name, value in item["case"]["expected_fields"].items()
    }
    predicted_pairs = {
        (item["case"]["case_id"], name, value)
        for item in results
        for name, value in item["accepted_fields"].items()
    }
    true_positive = len(expected_pairs & predicted_pairs)
    false_positive = len(predicted_pairs - expected_pairs)
    false_negative = len(expected_pairs - predicted_pairs)
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None
        and recall is not None
        and precision + recall
        else 0.0
        if precision is not None and recall is not None
        else None
    )

    date_cases = [
        item
        for item in results
        if item["case"].get("date_status")
    ]
    date_correct = 0
    for item in date_cases:
        case = item["case"]
        expected_status = case["date_status"]["visit_date"]
        fact = item["actual_fields"].get("visit_date")
        reason = (fact["review_reason"] or "").casefold() if fact else ""
        if expected_status == "invalid":
            correct = (
                fact is not None
                and "invalid calendar date" in reason
                and fact["extracted_value"]
                == case["expected_invalid_dates"]["visit_date"]
                and "visit_date" not in item["accepted_fields"]
            )
        elif expected_status == "ambiguous":
            correct = (
                fact is not None
                and fact["extracted_value"] == case["expected_fields"]["visit_date"]
                and "ambiguous" in reason
            )
        else:
            correct = (
                item["accepted_fields"].get("visit_date")
                == case["expected_fields"]["visit_date"]
                and "ambiguous" not in reason
            )
        date_correct += correct

    grounded_facts = 0
    total_facts = 0
    expected_evidence_total = 0
    expected_evidence_found = 0
    case_error_categories = defaultdict(Counter)
    failures = []
    by_quality: dict[str, list[dict]] = defaultdict(list)
    for item in results:
        case = item["case"]
        document = item["document"]
        raw_text = document["ocr_text"] or ""
        fields = item["actual_fields"]
        expected_type_signal = next(
            (line for line in case["lines"] if line.casefold() == "anc record"),
            None,
        )
        if expected_type_signal:
            expected_evidence_total += 1
            if _normalized(expected_type_signal) in _normalized(raw_text):
                expected_evidence_found += 1

        for name, expected_value in case["expected_fields"].items():
            fact = fields.get(name)
            quote = _expected_quote(case, name)
            quote_seen = bool(
                quote and _normalized(quote) in _normalized(raw_text)
            )
            if quote:
                expected_evidence_total += 1
                expected_evidence_found += quote_seen
            if fact and fact["source_text"]:
                total_facts += 1
                grounded_facts += (
                    _normalized(fact["source_text"]) in _normalized(raw_text)
                )
            if item["accepted_fields"].get(name) != expected_value:
                classification = (
                    "downstream_extraction"
                    if quote_seen
                    else "ocr_or_upstream"
                )
                case_error_categories[case["case_id"]][classification] += 1
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "quality_category": case["quality_category"],
                        "issue": "field_value_mismatch",
                        "field": name,
                        "expected": expected_value,
                        "actual": item["accepted_fields"].get(name),
                        "ocr_quote_seen": quote_seen,
                        "likely_error_stage": classification,
                    }
                )

        for name in case.get("expected_absent_fields", []):
            if name in item["accepted_fields"]:
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "quality_category": case["quality_category"],
                        "issue": "unexpected_field",
                        "field": name,
                        "actual": item["accepted_fields"][name],
                    }
                )
        for name, expected_value in case.get(
            "expected_invalid_dates", {}
        ).items():
            fact = fields.get(name)
            if not (
                fact
                and fact["extracted_value"] == expected_value
                and "invalid calendar date"
                in (fact["review_reason"] or "").casefold()
                and name not in item["accepted_fields"]
            ):
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "quality_category": case["quality_category"],
                        "issue": "impossible_date_not_rejected",
                        "actual": fact["extracted_value"] if fact else None,
                    }
                )
        if document["extraction_status"] != case.get(
            "expected_extraction_status", "extracted"
        ):
            failures.append(
                {
                    "case_id": case["case_id"],
                    "quality_category": case["quality_category"],
                    "issue": "extraction_status_mismatch",
                    "expected": case.get("expected_extraction_status", "extracted"),
                    "actual": document["extraction_status"],
                }
            )
        if (
            document["document_type"]
            != case["expected_document_type"]
            and document["extraction_status"] == "extracted"
        ):
            signal_seen = expected_type_signal and (
                _normalized(expected_type_signal) in _normalized(raw_text)
            )
            classification = (
                "downstream_classification" if signal_seen else "ocr_or_upstream"
            )
            case_error_categories[case["case_id"]][classification] += 1
            failures.append(
                {
                    "case_id": case["case_id"],
                    "quality_category": case["quality_category"],
                    "issue": "classification_mismatch",
                    "expected": case["expected_document_type"],
                    "actual": document["document_type"],
                    "expected_text_signal_seen": bool(signal_seen),
                    "likely_error_stage": classification,
                }
            )
        by_quality[case["quality_category"]].append(item)

    conflict_truth = {
        frozenset(
            item["document_id"]
            for item in results
            if item["case"].get("person_group") == group
        )
        for group in {
            item["case"]["person_group"]
            for item in results
            if item["case"].get("person_group")
        }
    }
    detected_conflict_sets = [
        frozenset(source["document_id"] for source in conflict["sources"])
        for item in results
        for conflict in item["link_conflicts"]
        if conflict["field_name"] == "blood_pressure"
    ]
    conflict_tp = sum(group in detected_conflict_sets for group in conflict_truth)
    conflict_fp = sum(group not in conflict_truth for group in detected_conflict_sets)
    conflict_fn = len(conflict_truth) - conflict_tp
    for item in results:
        if item["case"].get("person_group") and len(
            [
                candidate
                for candidate in results
                if candidate["case"].get("person_group")
                == item["case"]["person_group"]
            ]
        ) > 1 and item["case"]["case_id"] == "same_visit_pressure_b":
            group_ids = frozenset(
                candidate["document_id"]
                for candidate in results
                if candidate["case"].get("person_group")
                == item["case"]["person_group"]
            )
            if group_ids not in detected_conflict_sets:
                failures.append(
                    {
                        "case_id": item["case"]["person_group"],
                        "quality_category": "conflicting_values",
                        "issue": "same_person_same_visit_conflict_not_detected",
                    }
                )

    by_quality_metrics = {}
    for category, group in sorted(by_quality.items()):
        group_eligible = [
            item
            for item in group
            if item["document"]["extraction_status"] == "extracted"
        ]
        group_expected = {
            (item["case"]["case_id"], name, value)
            for item in group
            for name, value in item["case"]["expected_fields"].items()
        }
        group_predicted = {
            (item["case"]["case_id"], name, value)
            for item in group
            for name, value in item["accepted_fields"].items()
        }
        group_tp = len(group_expected & group_predicted)
        group_fp = len(group_predicted - group_expected)
        group_fn = len(group_expected - group_predicted)
        group_p = _ratio(group_tp, group_tp + group_fp)
        group_r = _ratio(group_tp, group_tp + group_fn)
        group_f1 = (
            2 * group_p * group_r / (group_p + group_r)
            if group_p is not None and group_r is not None and group_p + group_r
            else 0.0
            if group_p is not None and group_r is not None
            else None
        )
        by_quality_metrics[category] = {
            "documents": len(group),
            "ocr_failures": sum(
                item["document"]["extraction_status"] == "failed"
                for item in group
            ),
            "classification_correct": sum(
                item["document"]["document_type"]
                == item["case"]["expected_document_type"]
                for item in group_eligible
            ),
            "classification_denominator": len(group_eligible),
            "classification_accuracy": _ratio(
                sum(
                    item["document"]["document_type"]
                    == item["case"]["expected_document_type"]
                    for item in group_eligible
                ),
                len(group_eligible),
            ),
            "field_value_precision": group_p,
            "field_value_recall": group_r,
            "field_value_f1": group_f1,
            "expected_fields": len(group_expected),
            "predicted_fields": len(group_predicted),
        }

    unsupported_ok = (
        unsupported_response.status_code == unsupported["expected_http_status"]
    )
    if not unsupported_ok:
        failures.append(
            {
                "case_id": "unsupported_document",
                "quality_category": "unsupported",
                "issue": "unsupported_upload_not_rejected",
                "http_status": unsupported_response.status_code,
            }
        )
    ocr_failures = sum(
        item["document"]["extraction_status"] == "failed" for item in results
    )
    return {
        "dataset_id": dataset["dataset_id"],
        "dataset_composition": {
            "scan_documents": len(cases),
            "quality_categories": dict(
                Counter(case["quality_category"] for case in cases)
            ),
            "unsupported_upload_cases": 1,
            "generation_method": dataset["generation_method"],
            "font": str(font_path),
        },
        "execution": {
            "pipeline": "POST /documents -> TesseractOCRProvider -> rules -> persisted GET /documents/{id}",
            "provider": provider.name,
            "tesseract_version": tesseract_version,
            "tesseract_executable": executable_path,
            "model": "none; no model provider used",
            "all_images_generated_at_runtime": True,
            "dataset_size": len(results),
        },
        "metrics": {
            "classification": {
                "correct": correct_classifications,
                "denominator": len(eligible),
                "accuracy": _ratio(correct_classifications, len(eligible)),
                "confusion_matrix": confusion,
                "denominator_method": (
                    "Generated scans with extraction_status=extracted. "
                    "Unreadable scans are separately scored as extraction failures."
                ),
            },
            "field_level": {
                "true_positive": true_positive,
                "false_positive": false_positive,
                "false_negative": false_negative,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "denominator_method": (
                    "Micro-averaged exact (case_id, field_name, value) pairs. "
                    "Values explicitly flagged invalid are excluded as predictions."
                ),
            },
            "date_validation": {
                "correct": date_correct,
                "denominator": len(date_cases),
                "accuracy": _ratio(date_correct, len(date_cases)),
                "invalid_expected": sum(
                    case["date_status"]["visit_date"] == "invalid"
                    for case in (item["case"] for item in date_cases)
                ),
                "ambiguous_expected": sum(
                    case["date_status"]["visit_date"] == "ambiguous"
                    for case in (item["case"] for item in date_cases)
                ),
                "denominator_method": (
                    "Valid dates require exact extracted value without ambiguity; "
                    "ambiguous dates require explicit ambiguity review; impossible "
                    "dates require a flagged invalid candidate not accepted as valid."
                ),
            },
            "cross_document_conflicts": {
                "true_positive": conflict_tp,
                "false_positive": conflict_fp,
                "false_negative": conflict_fn,
                "precision": _ratio(conflict_tp, conflict_tp + conflict_fp),
                "recall": _ratio(conflict_tp, conflict_tp + conflict_fn),
                "expected_conflict_groups": len(conflict_truth),
                "detected_conflict_pairs": len(detected_conflict_sets),
                "denominator_method": (
                    "Same synthetic person_group, same document type, matching "
                    "unambiguous visit date and visit number; differing blood-pressure "
                    "values are the labeled conflict."
                ),
            },
            "evidence_grounding": {
                "grounded_facts": grounded_facts,
                "predicted_facts": total_facts,
                "accuracy": _ratio(grounded_facts, total_facts),
                "expected_source_quotes_found": expected_evidence_found,
                "expected_source_quotes": expected_evidence_total,
                "source_quote_recall": _ratio(
                    expected_evidence_found, expected_evidence_total
                ),
                "denominator_method": (
                    "Each persisted field source_text must be a normalized literal "
                    "substring of persisted OCR text; labeled source-line recall "
                    "separately checks expected labels/field lines."
                ),
            },
            "ocr_failures": {
                "failures": ocr_failures,
                "documents": len(results),
                "failure_rate": _ratio(ocr_failures, len(results)),
                "unreadable_case_failed": all(
                    item["document"]["extraction_status"] == "failed"
                    for item in results
                    if item["case"].get("unreadable")
                ),
            },
            "unsupported_upload": {
                "correct_rejections": int(unsupported_ok),
                "denominator": 1,
                "http_status": unsupported_response.status_code,
            },
            "by_quality_category": by_quality_metrics,
            "error_stage_counts": {
                case_id: dict(counter)
                for case_id, counter in case_error_categories.items()
            },
        },
        "failures": failures,
        "cases": [
            {
                "case_id": item["case"]["case_id"],
                "quality_category": item["case"]["quality_category"],
                "document_id": item["document_id"],
                "image_sha256": item["image_sha256"],
                "ocr_text": item["document"]["ocr_text"],
                "extraction_status": item["document"]["extraction_status"],
                "document_type": item["document"]["document_type"],
                "fields": [
                    {
                        "field_name": field["field_name"],
                        "extracted_value": field["extracted_value"],
                        "source_text": field["source_text"],
                        "needs_review": bool(field["needs_review"]),
                        "review_reason": field["review_reason"],
                    }
                    for field in item["fields"]
                ],
            }
            for item in results
        ],
        "limitations": [
            "Synthetic generated scans are a small engineering challenge set, not a representative clinical dataset.",
            "OCR rates should not be interpreted as performance on real handwritten records.",
            "A blank generated page tests no-text failure, not all unreadable-page conditions.",
            "No live model provider is used; classification and field extraction are deterministic rules.",
        ],
    }


def _run_isolated(
    font_path: Path,
    tesseract_executable: Path | None = None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="asha-real-ocr-eval-") as directory:
        root = Path(directory)
        document_dir = root / "documents"
        document_dir.mkdir()
        with (
            patch.dict(
                os.environ,
                {
                    "ASHA_ENV": "development",
                    "ASHA_DOCUMENT_PROVIDER": "tesseract",
                },
            ),
            patch.object(db, "DB_PATH", root / "evaluation.db"),
            patch.object(main, "DOCUMENT_DIR", document_dir),
        ):
            db.init_db()
            with TestClient(main.app) as client:
                return run_real_ocr_evaluation(
                    client,
                    font_path,
                    tesseract_executable,
                )


def main_cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--font",
        type=Path,
        help="Optional absolute path to a Windows TrueType font.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON report path; defaults to stdout.",
    )
    parser.add_argument(
        "--tesseract-executable",
        type=Path,
        help="Optional absolute path to tesseract.exe; configured for this process only.",
    )
    arguments = parser.parse_args()
    font_path = select_font(arguments.font)
    result = _run_isolated(font_path, arguments.tesseract_executable)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main_cli()
