from pathlib import Path

import pytesseract
from PIL import Image

from app.ocr.tesseract_provider import TesseractOCRProvider


def make_image(path: Path):
    Image.new("RGB", (120, 40), "white").save(path)
    return path


def test_tesseract_extracts_text_and_facts_when_engine_returns_text(tmp_path, monkeypatch):
    image = make_image(tmp_path / "record.png")
    monkeypatch.setattr(
        "app.ocr.tesseract_provider.pytesseract.image_to_string",
        lambda *_args, **_kwargs: "Name: Noor\nANC record",
    )
    monkeypatch.setattr(
        "app.ocr.tesseract_provider.pytesseract.image_to_data",
        lambda *_args, **_kwargs: {"conf": ["95", "-1"]},
    )

    result = TesseractOCRProvider().extract(str(image))

    assert result.extraction_status == "extracted"
    assert result.raw_text == "Name: Noor\nANC record"
    assert [fact.value for fact in result.facts if fact.field_name == "name"] == [
        "Noor"
    ]
    assert result.warnings == []


def test_blank_ocr_is_failed_and_has_no_facts(tmp_path, monkeypatch):
    image = make_image(tmp_path / "blank.png")
    monkeypatch.setattr(
        "app.ocr.tesseract_provider.pytesseract.image_to_string",
        lambda *_args, **_kwargs: " \n",
    )

    result = TesseractOCRProvider().extract(str(image))

    assert result.extraction_status == "failed"
    assert result.raw_text == ""
    assert result.facts == []
    assert "OCR returned no text" in result.warnings[0]


def test_missing_tesseract_is_failed_without_fabricated_facts(tmp_path, monkeypatch):
    image = make_image(tmp_path / "record.png")

    def missing_engine(*_args, **_kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(
        "app.ocr.tesseract_provider.pytesseract.image_to_string",
        missing_engine,
    )

    result = TesseractOCRProvider().extract(str(image))

    assert result.extraction_status == "failed"
    assert result.facts == []
    assert "not installed" in result.warnings[0]


def test_invalid_image_is_failed(tmp_path):
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not an image")

    result = TesseractOCRProvider().extract(str(invalid))

    assert result.extraction_status == "failed"
    assert result.facts == []
    assert "Image OCR failed" in result.warnings[0]


def test_unseen_name_is_not_replaced_with_sample_name(tmp_path, monkeypatch):
    sample = Path(__file__).resolve().parents[1] / "test_asha_record.png"
    image = tmp_path / "copied_sample.png"
    image.write_bytes(sample.read_bytes())
    monkeypatch.setattr(
        "app.ocr.tesseract_provider.pytesseract.image_to_string",
        lambda *_args, **_kwargs: "Name: Noor",
    )
    monkeypatch.setattr(
        "app.ocr.tesseract_provider.pytesseract.image_to_data",
        lambda *_args, **_kwargs: {"conf": ["90"]},
    )

    result = TesseractOCRProvider().extract(str(image))

    names = [fact.value for fact in result.facts if fact.field_name == "name"]
    assert result.extraction_status == "extracted"
    assert names == ["Noor"]
    assert "Rukhsana" not in names


def test_missing_file_is_failed():
    result = TesseractOCRProvider().extract("does_not_exist.png")

    assert result.document_type == "unknown"
    assert result.provider == "tesseract"
    assert result.extraction_status == "failed"
    assert result.raw_text == ""
    assert result.facts == []
    assert "File not found" in result.warnings[0]
