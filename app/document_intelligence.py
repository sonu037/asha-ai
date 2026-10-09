import json
import math
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .classification import DOCUMENT_TYPES
from .schema import ExtractionEnvelope
from .ocr.tesseract_provider import TesseractOCRProvider


SUPPORTED_DOCUMENT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
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
                classification_needs_review=True,
                provider=self.name,
                processing_version="0.3.0",
                warnings=[
                    f"Unsupported document type: "
                    f"{suffix or 'no extension'}"
                ],
                extraction_status="failed",
            )

        # Supported document, but real OCR/vision is not configured yet.
        return ExtractionEnvelope(
            document_type="unknown",
            language="und",
            classification_needs_review=True,
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
            extraction_status="failed",
        )


tesseract_provider = TesseractOCRProvider()
provider = MockDocumentIntelligenceProvider()


class OpenAICompatibleDocumentProvider(DocumentIntelligenceProvider):
    """Optional text-extraction adapter; all returned facts require human review."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        ocr_provider: TesseractOCRProvider | None = None,
        timeout_seconds: float = 30,
    ):
        parsed_url = urlparse(base_url)
        if parsed_url.scheme != "https" and parsed_url.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("External AI base URLs must use HTTPS.")
        if not parsed_url.netloc or not model.strip() or not api_key.strip():
            raise ValueError("AI base URL, model, and API key are required.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.ocr_provider = ocr_provider or tesseract_provider
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _normalized(text: str) -> str:
        return " ".join(text.casefold().split())

    def _failure(
        self,
        warning: str,
        source: ExtractionEnvelope | None = None,
    ) -> ExtractionEnvelope:
        return ExtractionEnvelope(
            document_type="unknown",
            language=source.language if source else None,
            raw_text=source.raw_text if source else "",
            provider=self.name,
            model=self.model,
            processing_version="0.3.0",
            page_count=source.page_count if source else None,
            extraction_status="failed",
            classification_needs_review=True,
            warnings=[*(source.warnings if source else []), warning],
        )

    def extract(self, path: str) -> ExtractionEnvelope:
        ocr_result = self.ocr_provider.extract(path)
        if ocr_result.extraction_status != "extracted" or not ocr_result.raw_text.strip():
            return ocr_result.model_copy(
                update={
                    "provider": self.name,
                    "model": self.model,
                    "classification_needs_review": True,
                }
            )
        if len(ocr_result.raw_text) > 30_000:
            return self._failure(
                "OCR text exceeds the 30000-character external processing limit.",
                ocr_result,
            )

        request_body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Classify this ASHA health-record OCR text and extract only "
                        "explicitly present facts. Return one JSON object with "
                        "document_type, classification_confidence (0 to 1), language, "
                        "and facts. Each fact must contain "
                        "field_name, value, confidence (0 to 1), and source_text as "
                        "an exact verbatim quote from the OCR text. Never infer missing "
                        "values. Use document_type unknown when uncertain."
                    ),
                },
                {"role": "user", "content": ocr_result.raw_text},
            ],
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=request_body,
                )
                response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict)
                )
            model_output = json.loads(content)
            if not isinstance(model_output, dict):
                raise ValueError("The AI response must be a JSON object.")
            document_type = model_output.get("document_type", "unknown")
            if document_type not in DOCUMENT_TYPES:
                raise ValueError("The AI returned an unsupported document type.")
            classification_confidence = model_output.get(
                "classification_confidence"
            )
            if (
                not isinstance(classification_confidence, (int, float))
                or isinstance(classification_confidence, bool)
                or not math.isfinite(classification_confidence)
                or not 0 <= classification_confidence <= 1
            ):
                classification_confidence = None
            raw_facts = model_output.get("facts", [])
            if not isinstance(raw_facts, list):
                raise ValueError("The AI facts value must be a list.")
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            return self._failure(
                f"External AI extraction failed ({type(exc).__name__}).",
                ocr_result,
            )

        normalized_ocr = self._normalized(ocr_result.raw_text)
        facts = []
        warnings = list(ocr_result.warnings)
        for raw_fact in raw_facts:
            if not isinstance(raw_fact, dict):
                warnings.append("An invalid AI fact was omitted.")
                continue
            field_name = raw_fact.get("field_name")
            value = raw_fact.get("value")
            confidence = raw_fact.get("confidence")
            source_text = raw_fact.get("source_text")
            if (
                not isinstance(field_name, str)
                or not field_name.strip()
                or isinstance(value, bool)
                or not isinstance(value, (str, int, float))
                or (isinstance(value, float) and not math.isfinite(value))
                or not isinstance(confidence, (int, float))
                or isinstance(confidence, bool)
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
                or not isinstance(source_text, str)
                or not source_text.strip()
                or self._normalized(source_text) not in normalized_ocr
                or self._normalized(str(value))
                not in self._normalized(source_text)
            ):
                warnings.append(
                    "An AI fact without valid, matching source evidence was omitted."
                )
                continue
            facts.append(
                {
                    "field_name": field_name.strip(),
                    "value": value,
                    "confidence": float(confidence),
                    "source_text": source_text,
                    "method": "ai",
                    "needs_review": True,
                    "reason": "AI-extracted value requires human verification.",
                }
            )
        language = model_output.get("language")
        if (
            model_output.get("classification_confidence") is not None
            and classification_confidence is None
        ):
            warnings.append(
                "AI classification confidence was invalid and was omitted."
            )
        if language is not None and not isinstance(language, str):
            language = None
            warnings.append("AI language metadata was invalid and was omitted.")
        return ExtractionEnvelope(
            document_type=document_type,
            classification_confidence=classification_confidence,
            classification_needs_review=True,
            language=language or ocr_result.language,
            raw_text=ocr_result.raw_text,
            facts=facts,
            provider=self.name,
            model=self.model,
            processing_version="0.3.0",
            page_count=ocr_result.page_count,
            extraction_status="extracted",
            warnings=warnings,
        )


def configured_provider() -> DocumentIntelligenceProvider:
    provider_name = os.environ.get("ASHA_DOCUMENT_PROVIDER", "tesseract").strip()
    if provider_name == "tesseract":
        return tesseract_provider
    if provider_name == "mock":
        return MockDocumentIntelligenceProvider()
    if provider_name != "openai_compatible":
        raise ValueError(
            "ASHA_DOCUMENT_PROVIDER must be tesseract, mock, or openai_compatible."
        )
    if os.environ.get("ASHA_ALLOW_EXTERNAL_AI", "").casefold() != "true":
        raise ValueError(
            "Set ASHA_ALLOW_EXTERNAL_AI=true only after approving external processing."
        )
    base_url = os.environ.get("ASHA_AI_BASE_URL", "").strip()
    api_key = os.environ.get("ASHA_AI_API_KEY", "").strip()
    model = os.environ.get("ASHA_AI_MODEL", "").strip()
    return OpenAICompatibleDocumentProvider(base_url, api_key, model)