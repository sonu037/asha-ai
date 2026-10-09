import hashlib
from io import BytesIO

import pytesseract
from PIL import Image

from scripts.evaluate_real_ocr import (
    generate_scan,
    load_dataset,
    select_font,
)


def test_generated_scan_dataset_is_synthetic_and_reproducible():
    dataset = load_dataset()
    font = select_font()
    cases = {case["case_id"]: case for case in dataset["cases"]}

    assert len(cases) == 13
    assert "synthetic" in dataset["generation_method"].casefold()

    for case_id in ("clear_standard", "slightly_skewed", "jpeg_compressed"):
        case = cases[case_id]
        first, suffix, content_type = generate_scan(case, font)
        second, repeated_suffix, repeated_content_type = generate_scan(case, font)
        assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()
        assert suffix == repeated_suffix
        assert content_type == repeated_content_type
        with Image.open(BytesIO(first)) as image:
            assert image.width == case["width"]
            assert image.height == case["height"]


def test_real_ocr_preflight_uses_process_local_executable(monkeypatch, tmp_path):
    from scripts.evaluate_real_ocr import _require_tesseract

    monkeypatch.setenv("ASHA_DOCUMENT_PROVIDER", "tesseract")
    executable = tmp_path / "synthetic-tesseract.exe"
    executable.write_bytes(b"synthetic executable placeholder")
    monkeypatch.setattr(
        pytesseract.pytesseract,
        "tesseract_cmd",
        pytesseract.pytesseract.tesseract_cmd,
    )
    monkeypatch.setattr(
        pytesseract,
        "get_tesseract_version",
        lambda: "synthetic-test-version",
    )
    from app.ocr.tesseract_provider import TesseractOCRProvider
    import scripts.evaluate_real_ocr as evaluator

    monkeypatch.setattr(
        evaluator,
        "configured_provider",
        lambda: TesseractOCRProvider(),
    )
    provider, version, executable_path = _require_tesseract(executable)

    assert provider.name == "tesseract"
    assert version == "synthetic-test-version"
    assert executable_path == str(executable)
    assert pytesseract.pytesseract.tesseract_cmd == str(executable)
