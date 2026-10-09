import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentClassification:
    document_type: str
    confidence: float
    method: str = "rule"
    needs_review: bool = False
    ambiguous: bool = False
    reason: str | None = None


DOCUMENT_TYPES = {
    "anc_record",
    "immunization_record",
    "home_visit",
    "ncd_screening",
    "pregnancy_record",
    "vhnd_record",
    "inventory_record",
    "asha_diary",
    "unknown",
}


def classify_document(text: str) -> DocumentClassification:
    """
    First-pass ASHA document classifier.

    This version is intentionally deterministic.
    Later, an AI classifier can supplement these rules.
    """

    normalized = " ".join(text.lower().split())

    if not normalized:
        return DocumentClassification(
            document_type="unknown",
            confidence=0.0,
            method="rule",
            needs_review=True,
        )

    keyword_groups = {
        "anc_record": ("anc", "antenatal", "pregnancy check"),
        "immunization_record": (
            "immunization",
            "immunisation",
            "vaccination",
            "bcg",
            "opv",
            "pentavalent",
            "measles",
        ),
        "home_visit": ("home visit", "visit conducted"),
        "ncd_screening": (
            "ncd",
            "hypertension",
            "diabetes",
            "screening",
        ),
        "pregnancy_record": (
            "pregnant",
            "pregnancy",
            "lmp",
            "expected date of delivery",
            "edd",
        ),
        "vhnd_record": ("vhnd", "village health", "nutrition day"),
        "inventory_record": (
            "stock",
            "inventory",
            "drug kit",
            "quantity received",
            "balance",
        ),
        "asha_diary": ("asha diary", "work diary"),
    }
    matched_types = [
        document_type
        for document_type, keywords in keyword_groups.items()
        if any(
            re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", normalized)
            for keyword in keywords
        )
    ]
    if len(matched_types) != 1:
        return DocumentClassification(
            document_type="unknown",
            confidence=0.0 if matched_types else 0.20,
            method="rule",
            needs_review=True,
            ambiguous=len(matched_types) > 1,
            reason=(
                "Conflicting document-type signals: "
                + ", ".join(sorted(matched_types))
                + ". Human classification review is required."
                if matched_types
                else "No supported document-type signal was found."
            ),
        )
    return DocumentClassification(
        document_type=matched_types[0],
        confidence=0.5,
        method="rule",
        needs_review=True,
    )