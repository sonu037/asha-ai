from pathlib import Path

import pytesseract
from PIL import Image
from pytesseract import Output

from ..classification import classify_document
from ..extraction import extract_basic_facts
from ..schema import ExtractionEnvelope


class TesseractOCRProvider:
    """Local OCR provider. Extraction failures remain explicit and produce no facts."""

    name = "tesseract"

    def __init__(self, language: str = "eng"):
        self.language = language

    def _failure(self, warning: str) -> ExtractionEnvelope:
        return ExtractionEnvelope(
            document_type="unknown",
            provider=self.name,
            model="tesseract",
            processing_version="0.3.0",
            extraction_status="failed",
            warnings=[warning],
        )

    def extract(self, path: str) -> ExtractionEnvelope:
        file_path = Path(path)
        if not file_path.is_file():
            return self._failure("File not found or is not a regular file.")

        try:
            with Image.open(file_path) as image:
                image.load()
                text = pytesseract.image_to_string(image, lang=self.language)
                if not text.strip():
                    return self._failure("OCR returned no text.")

                warnings = []
                try:
                    ocr_data = pytesseract.image_to_data(
                        image,
                        lang=self.language,
                        output_type=Output.DICT,
                    )
                    confidences = []
                    for confidence in ocr_data.get("conf", []):
                        try:
                            value = float(confidence)
                        except (TypeError, ValueError):
                            continue
                        if value >= 0:
                            confidences.append(value)
                    if confidences:
                        average_confidence = sum(confidences) / len(confidences)
                        if average_confidence < 60:
                            warnings.append(
                                "Low average OCR confidence; human review recommended."
                            )
                        elif average_confidence < 80:
                            warnings.append(
                                "Moderate OCR confidence; review extracted fields."
                            )
                except Exception as exc:
                    warnings.append(
                        f"OCR confidence data unavailable: {type(exc).__name__}."
                    )

                classification = classify_document(text)
                if classification.needs_review:
                    warnings.append(
                        "Document type could not be classified confidently."
                    )
                facts = extract_basic_facts(text)
                return ExtractionEnvelope(
                    document_type=classification.document_type,
                    language=self.language,
                    raw_text=text,
                    facts=facts,
                    provider=self.name,
                    model="tesseract",
                    processing_version="0.3.0",
                    page_count=1,
                    extraction_status="extracted",
                    warnings=warnings,
                )
        except pytesseract.TesseractNotFoundError:
            return self._failure(
                "Tesseract executable is not installed or is not available on PATH."
            )
        except Exception as exc:
            return self._failure(
                f"Image OCR failed ({type(exc).__name__})."
            )


provider = TesseractOCRProvider()
