from pathlib import Path

from app.ocr.tesseract_provider import TesseractOCRProvider


TEST_IMAGE = Path("test_asha_record.png")


def test_tesseract_extracts_raw_text():
    provider = TesseractOCRProvider()

    result = provider.extract(str(TEST_IMAGE))

    assert result.provider == "tesseract"
    assert result.model == "tesseract"
    assert result.raw_text.strip()
    assert result.warnings


def test_tesseract_extracts_reliable_name():
    provider = TesseractOCRProvider()

    result = provider.extract(str(TEST_IMAGE))

    names = [
        fact
        for fact in result.facts
        if fact.field_name == "name"
    ]

    assert len(names) == 1
    assert names[0].value == "Rukhsana"
    assert names[0].method == "rule"


def test_tesseract_handles_missing_file():
    provider = TesseractOCRProvider()

    result = provider.extract("does_not_exist.png")

    assert result.document_type == "unknown"
    assert result.provider == "tesseract"
    assert result.raw_text == ""
    assert "File not found" in result.warnings[0]