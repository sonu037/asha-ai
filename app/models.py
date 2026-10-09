from pydantic import BaseModel, Field
from typing import Any, Optional


class HouseholdCreate(BaseModel):
    external_id: str
    village_id: Optional[str] = None
    address: Optional[str] = None


class PersonCreate(BaseModel):
    external_id: str
    household_id: int
    name: str
    sex: Optional[str] = None
    date_of_birth: Optional[str] = None
    relationship_to_head: Optional[str] = None


class EventCreate(BaseModel):
    external_id: str
    household_id: Optional[int] = None
    person_id: Optional[int] = None
    asha_id: Optional[str] = None
    event_type: str
    event_date: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)
    source_document_id: Optional[int] = None
    verification_status: str = "unverified"
    verified_by: Optional[str] = None
    verified_at: Optional[str] = None

class FieldVerification(BaseModel):
    verified_value: str
    verified_by: str


class CanonicalRecordCreate(BaseModel):
    person_id: Optional[int] = None
    household_id: Optional[int] = None
    record_type: str
    field_name: str
    value: str
    verified_by: str
    source_document_id: Optional[int] = None
class DocumentPersonLink(BaseModel):
    person_id: int
    linked_by: str
class DocumentEventCreate(BaseModel):
    person_id: int
    event_type: str
    event_date: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)
    verified_by: str


class AssistantQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class ClassificationVerification(BaseModel):
    document_type: str
    verified_by: str