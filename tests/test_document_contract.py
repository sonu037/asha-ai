from app.document_intelligence import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    provider,
)


def test_document_provider_contract_for_supported_image():
    result = provider.extract("/tmp/example.jpg")

    assert result.document_type == "unknown"
    assert result.provider == "mock"
    assert result.processing_version == "0.3.0"
    assert result.facts == []
    assert result.warnings
    assert result.extraction_status == "failed"
    assert result.classification_needs_review is True


def test_document_provider_rejects_unsupported_file():
    result = provider.extract("/tmp/example.txt")

    assert result.document_type == "unknown"
    assert result.provider == "mock"
    assert result.facts == []
    assert "Unsupported document type" in result.warnings[0]


def test_supported_document_extensions_are_explicit():
    assert ".jpg" in SUPPORTED_DOCUMENT_EXTENSIONS
    assert ".webp" in SUPPORTED_DOCUMENT_EXTENSIONS
    assert ".pdf" not in SUPPORTED_DOCUMENT_EXTENSIONS
    assert ".heic" not in SUPPORTED_DOCUMENT_EXTENSIONS
    assert ".txt" not in SUPPORTED_DOCUMENT_EXTENSIONS