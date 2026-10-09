from pydantic import BaseModel, Field
from typing import Any, Literal, Optional


class ExtractedFact(BaseModel):
    field_name: str
    value: Any = None
    confidence: float = Field(ge=0, le=1)
    page_number: Optional[int] = None
    bounding_box: Optional[list[float]] = None
    source_text: Optional[str] = None
    method: Literal["ocr", "rule", "ai", "manual"] = "ai"
    needs_review: bool = False
    reason: Optional[str] = None


class ExtractionEnvelope(BaseModel):
    document_type: str
    classification_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    classification_needs_review: bool = False
    language: Optional[str] = None
    raw_text: str = ""
    facts: list[ExtractedFact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    extraction_status: Literal["extracted", "failed"] = "extracted"

    # Provenance / reproducibility
    provider: str = "unknown"
    model: Optional[str] = None
    processing_version: str = "0.3.0"
    page_count: Optional[int] = None


class TimelineItem(BaseModel):
    event_type: str
    event_date: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    source_document_id: Optional[int] = None