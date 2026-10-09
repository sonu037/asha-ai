from pathlib import Path
from .schema import ExtractionEnvelope
from .ocr.tesseract_provider import TesseractOCRProvider


SUPPORTED_DOCUMENT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".pdf",
}


class DocumentIntelligenceProvider:
    """
    Stable interface for OCR/vision/AI document extraction providers.

    Real OCR/vision providers will implement this same contract later.
    """

    name = "base"
    model = None

    def extract(self, path: str) -> ExtractionEnvelope:
        raise NotImplementedError


class MockDocumentIntelligenceProvider(DocumentIntelligenceProvider):
    """
    Safe deterministic provider used until a real OCR/AI adapter
    is configured.

    IMPORTANT:
    This provider does NOT pretend to perform OCR.
    """

    name = "mock"

    def extract(self, path: str) -> ExtractionEnvelope:
        suffix = Path(path).suffix.lower()

        # Reject unsupported files explicitly.
        if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
            return ExtractionEnvelope(
                document_type="unknown",
                provider=self.name,
                processing_version="0.3.0",
                warnings=[
                    f"Unsupported document type: "
                    f"{suffix or 'no extension'}"
                ],
            )

        # Supported document, but real OCR/vision is not configured yet.
        return ExtractionEnvelope(
            document_type="unknown_health_record",
            language="und",
            raw_text="",
            facts=[],
            provider=self.name,
            model=None,
            processing_version="0.3.0",
            page_count=None,
            warnings=[
                "OCR/vision provider not configured; "
                "document is stored safely for later extraction."
            ],
        )


provider = MockDocumentIntelligenceProvider()
tesseract_provider = TesseractOCRProvider()