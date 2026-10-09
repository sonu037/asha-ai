import re

from .schema import ExtractedFact


def _add_fact(
    facts: list[ExtractedFact],
    field_name: str,
    value,
    source_text: str,
    confidence: float,
    needs_review: bool = False,
    reason: str | None = None,
):
    facts.append(
        ExtractedFact(
            field_name=field_name,
            value=value,
            confidence=confidence,
            source_text=source_text,
            method="rule",
            needs_review=needs_review,
            reason=reason,
        )
    )


def extract_basic_facts(text: str) -> list[ExtractedFact]:
    """
    Conservative rule-based extraction for common ASHA/ANC fields.

    The extractor accepts only relatively clear patterns.
    It does not invent values when OCR evidence is weak.
    """

    facts: list[ExtractedFact] = []

    # ---------------------------------------------------------
    # NAME
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*name\s*[:\-]?\s*(.+?)\s*$",
        text,
    )
    if match:
        _add_fact(
            facts,
            field_name="name",
            value=match.group(1).strip(),
            source_text=match.group(0).strip(),
            confidence=0.99,
        )

    # ---------------------------------------------------------
    # AGE
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*age\s*[:\-]?\s*(\d{1,3})\s*$",
        text,
    )
    if match:
        age = int(match.group(1))

        if 0 <= age <= 120:
            _add_fact(
                facts,
                field_name="age",
                value=age,
                source_text=match.group(0).strip(),
                confidence=0.99,
            )

    # ---------------------------------------------------------
    # VILLAGE
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*vill?age\s*[:\-]?\s*(.+?)\s*$",
        text,
    )
    if match:
        _add_fact(
            facts,
            field_name="village",
            value=match.group(1).strip(),
            source_text=match.group(0).strip(),
            confidence=0.97,
        )

    # ---------------------------------------------------------
    # ANC VISIT
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*anc\s+visit\s*[:\-]?\s*(\d{1,2})\s*$",
        text,
    )
    if match:
        visit_number = int(match.group(1))

        if 1 <= visit_number <= 10:
            _add_fact(
                facts,
                field_name="anc_visit",
                value=visit_number,
                source_text=match.group(0).strip(),
                confidence=0.99,
            )

    # ---------------------------------------------------------
    # VISIT DATE
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*(?:visit\s+date|date\s+of\s+visit|event\s+date)\s*"
        r"[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*$",
        text,
    )
    if match:
        _add_fact(
            facts,
            field_name="visit_date",
            value=match.group(1),
            source_text=match.group(0).strip(),
            confidence=0.98,
        )

    # ---------------------------------------------------------
    # LMP - Last Menstrual Period
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*(?:lmp|last\s+menstrual\s+period)\s*"
        r"[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*$",
        text,
    )

    if match:
        _add_fact(
            facts,
            field_name="lmp",
            value=match.group(1),
            source_text=match.group(0).strip(),
            confidence=0.98,
        )

    # ---------------------------------------------------------
    # EDD - Expected Date of Delivery
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*(?:edd|expected\s+date\s+of\s+delivery)\s*"
        r"[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*$",
        text,
    )

    if match:
        _add_fact(
            facts,
            field_name="edd",
            value=match.group(1),
            source_text=match.group(0).strip(),
            confidence=0.98,
        )

    # ---------------------------------------------------------
    # BLOOD PRESSURE
    # Example:
    # BP: 120/80
    # Blood Pressure: 110/70
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*(?:bp|blood\s+pressure)\s*"
        r"[:\-]?\s*"
        r"(\d{2,3}\s*/\s*\d{2,3})\s*$",
        text,
    )

    if match:
        bp = re.sub(r"\s+", "", match.group(1))

        systolic, diastolic = map(int, bp.split("/"))

        if 50 <= systolic <= 250 and 30 <= diastolic <= 150:
            _add_fact(
                facts,
                field_name="blood_pressure",
                value=bp,
                source_text=match.group(0).strip(),
                confidence=0.98,
            )

    # ---------------------------------------------------------
    # WEIGHT
    # Example:
    # Weight: 52 kg
    # Weight: 52.5 kg
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*weight\s*[:\-]?\s*"
        r"(\d{2,3}(?:\.\d{1,2})?)\s*(?:kg|kgs)?\s*$",
        text,
    )

    if match:
        weight = float(match.group(1))

        if 20 <= weight <= 200:
            value = int(weight) if weight.is_integer() else weight

            _add_fact(
                facts,
                field_name="weight_kg",
                value=value,
                source_text=match.group(0).strip(),
                confidence=0.97,
            )

    # ---------------------------------------------------------
    # HEMOGLOBIN
    # Example:
    # Hb: 11.2
    # Hemoglobin: 10.5 g/dL
    # ---------------------------------------------------------
    match = re.search(
        r"(?im)^\s*(?:hb|hemoglobin)\s*[:\-]?\s*"
        r"(\d{1,2}(?:\.\d{1,2})?)\s*"
        r"(?:g\s*/\s*dl)?\s*$",
        text,
    )

    if match:
        hemoglobin = float(match.group(1))

        if 2 <= hemoglobin <= 25:
            value = (
                int(hemoglobin)
                if hemoglobin.is_integer()
                else hemoglobin
            )

            _add_fact(
                facts,
                field_name="hemoglobin",
                value=value,
                source_text=match.group(0).strip(),
                confidence=0.97,
            )

    return facts