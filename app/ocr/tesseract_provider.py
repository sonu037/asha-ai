from pathlib import Path

import pytesseract
from PIL import Image
from pytesseract import Output

from ..classification import classify_document
from ..extraction import extract_basic_facts
from ..schema import ExtractionEnvelope


class TesseractOCRProvider:
    """
    Local OCR/document-intelligence provider.

    Pipeline:
        image
          ↓
        Tesseract OCR
          ↓
        document classification
          ↓
        structured field extraction
    """

    name = "tesseract"

    def __init__(self, language: str = "eng"):
        self.language = language

    def extract(self, path: str) -> ExtractionEnvelope:
        file_path = Path(path)

        if not file_path.exists():
            return ExtractionEnvelope(
                document_type="unknown",
                provider=self.name,
                model="tesseract",
                processing_version="0.3.0",
                warnings=[f"File not found: {file_path}"],
            )

        try:
            image = Image.open(file_path)

            # -------------------------------------------------
            # 1. OCR
            # -------------------------------------------------
            text = pytesseract.image_to_string(
                image,
                lang=self.language,
            )

            # -------------------------------------------------
            # 2. OCR confidence
            # -------------------------------------------------
            ocr_data = pytesseract.image_to_data(
                image,
                lang=self.language,
                output_type=Output.DICT,
            )

            confidences = []

            for confidence in ocr_data["conf"]:
                try:
                    value = float(confidence)

                    if value >= 0:
                        confidences.append(value)

                except (ValueError, TypeError):
                    continue

            average_ocr_confidence = (
                sum(confidences) / len(confidences)
                if confidences
                else None
            )

            # -------------------------------------------------
            # 3. Document classification
            # -------------------------------------------------
            classification = classify_document(text)

            # -------------------------------------------------
            # 4. Structured extraction
            # -------------------------------------------------
            facts = extract_basic_facts(text)

            # -------------------------------------------------
            # 5. Warnings
            # -------------------------------------------------
            warnings = []

            if average_ocr_confidence is not None:
                if average_ocr_confidence < 60:
                    warnings.append(
                        "Low average OCR confidence; "
                        "human review recommended."
                    )

                elif average_ocr_confidence < 80:
                    warnings.append(
                        "Moderate OCR confidence; "
                        "review extracted fields."
                    )

            if classification.needs_review:
                warnings.append(
                    "Document type could not be classified confidently."
                )

            # -------------------------------------------------
            # 6. Final extraction envelope
            # -------------------------------------------------
            return ExtractionEnvelope(
                document_type=classification.document_type,
                language=self.language,
                raw_text=text,
                facts=facts,
                provider=self.name,
                model="tesseract",
                processing_version="0.3.0",
                page_count=1,
                warnings=warnings,
            )

        except Exception as exc:
            return ExtractionEnvelope(
                document_type="unknown",
                provider=self.name,
                model="tesseract",
                processing_version="0.3.0",
                warnings=[f"OCR failed: {exc}"],
            )


provider = TesseractOCRProvider()