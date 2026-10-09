from app.extraction import extract_basic_facts


def facts_by_name(text: str):
    return {
        fact.field_name: fact
        for fact in extract_basic_facts(text)
    }


def test_anc_fields_are_extracted():
    text = """
    Name: Rukhsana
    Age: 27
    Village: Mantrigam
    ANC Visit: 3
    LMP: 15/06/2026
    EDD: 22/03/2027
    BP: 120/80
    Weight: 52.5 kg
    Hb: 11.2 g/dL
    """

    facts = facts_by_name(text)

    assert facts["name"].value == "Rukhsana"
    assert facts["age"].value == 27
    assert facts["village"].value == "Mantrigam"
    assert facts["anc_visit"].value == 3
    assert facts["lmp"].value == "15/06/2026"
    assert facts["edd"].value == "22/03/2027"
    assert facts["blood_pressure"].value == "120/80"
    assert facts["weight_kg"].value == 52.5
    assert facts["hemoglobin"].value == 11.2


def test_invalid_anc_values_are_rejected():
    text = """
    Age: 250
    ANC Visit: 15
    BP: 500/500
    Weight: 500 kg
    Hb: 100 g/dL
    """

    facts = facts_by_name(text)

    assert "age" not in facts
    assert "anc_visit" not in facts
    assert "blood_pressure" not in facts
    assert "weight_kg" not in facts
    assert "hemoglobin" not in facts