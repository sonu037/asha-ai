from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentClassification:
    document_type: str
    confidence: float
    method: str = "rule"
    needs_review: bool = False


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

    # ANC / antenatal care
    if any(
        keyword in normalized
        for keyword in (
            "anc",
            "antenatal",
            "antenatal care",
            "pregnancy check",
        )
    ):
        return DocumentClassification(
            document_type="anc_record",
            confidence=0.90,
            method="rule",
        )

    # Immunization
    if any(
        keyword in normalized
        for keyword in (
            "immunization",
            "immunisation",
            "vaccination",
            "bcg",
            "opv",
            "pentavalent",
            "measles",
        )
    ):
        return DocumentClassification(
            document_type="immunization_record",
            confidence=0.90,
            method="rule",
        )

    # Home visit
    if any(
        keyword in normalized
        for keyword in (
            "home visit",
            "home visit date",
            "visit conducted",
        )
    ):
        return DocumentClassification(
            document_type="home_visit",
            confidence=0.90,
            method="rule",
        )

    # NCD screening
    if any(
        keyword in normalized
        for keyword in (
            "ncd",
            "blood pressure",
            "hypertension",
            "diabetes",
            "screening",
        )
    ):
        return DocumentClassification(
            document_type="ncd_screening",
            confidence=0.90,
            method="rule",
        )

    # Pregnancy
    if any(
        keyword in normalized
        for keyword in (
            "pregnant",
            "pregnancy",
            "lmp",
            "expected date of delivery",
            "edd",
        )
    ):
        return DocumentClassification(
            document_type="pregnancy_record",
            confidence=0.90,
            method="rule",
        )

    # VHND
    if any(
        keyword in normalized
        for keyword in (
            "vhnd",
            "village health",
            "nutrition day",
        )
    ):
        return DocumentClassification(
            document_type="vhnd_record",
            confidence=0.90,
            method="rule",
        )

    # Inventory / stock
    if any(
        keyword in normalized
        for keyword in (
            "stock",
            "inventory",
            "drug kit",
            "quantity received",
            "balance",
        )
    ):
        return DocumentClassification(
            document_type="inventory_record",
            confidence=0.90,
            method="rule",
        )

    # ASHA diary
    if any(
        keyword in normalized
        for keyword in (
            "asha diary",
            "asha diary entry",
            "work diary",
        )
    ):
        return DocumentClassification(
            document_type="asha_diary",
            confidence=0.90,
            method="rule",
        )

    return DocumentClassification(
        document_type="unknown",
        confidence=0.20,
        method="rule",
        needs_review=True,
    )