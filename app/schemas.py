"""Pydantic v2 schemas used for request validation and response serialization."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import PitchStatus


class DomainInput(BaseModel):
    """A raw domain submitted by a client, normalized for downstream lookups."""

    domain: str = Field(..., min_length=3, max_length=255, examples=["acme.com"])

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        value = value.strip().lower()
        value = value.removeprefix("https://").removeprefix("http://").removeprefix("www.")
        value = value.split("/")[0]
        if "." not in value:
            raise ValueError("Invalid domain format")
        return value


class CompanyCreate(BaseModel):
    """Payload for creating/enriching a Company record."""

    domain: str = Field(..., min_length=3, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=255)
    company_size: str | None = Field(default=None, max_length=50)
    raw_scraped_text: str | None = None

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        return DomainInput.normalize_domain(value)


class CompanyResponse(BaseModel):
    """Serialized Company record returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    domain: str
    company_name: str | None
    industry: str | None
    company_size: str | None
    created_at: datetime


class LeadPitchCreate(BaseModel):
    """Payload for requesting a new personalized pitch for a company."""

    company_id: uuid.UUID


class LeadPitchResponse(BaseModel):
    """Serialized LeadPitch record, including generation status."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    generated_pitch: str | None
    status: PitchStatus
    created_at: datetime
