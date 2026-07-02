"""API routes for company enrichment and lead pitch generation."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Company, LeadPitch, PitchStatus
from app.schemas import CompanyCreate, CompanyResponse, LeadPitchResponse
from app.worker import enrich_company_task

router = APIRouter()


@router.post("/companies", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
def create_company(payload: CompanyCreate, db: Session = Depends(get_db)) -> Company:
    """Create (or reject a duplicate of) a Company record."""
    existing = db.query(Company).filter(Company.domain == payload.domain).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Company with domain '{payload.domain}' already exists",
        )

    company = Company(**payload.model_dump())
    try:
        db.add(company)
        db.commit()
        db.refresh(company)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist company record",
        ) from exc

    return company


@router.post(
    "/companies/{company_id}/pitches",
    response_model=LeadPitchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_lead_pitch(company_id: uuid.UUID, db: Session = Depends(get_db)) -> LeadPitch:
    """Queue a background job to generate a personalized pitch for a company."""
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    pitch = LeadPitch(company_id=company.id, status=PitchStatus.PENDING)
    try:
        db.add(pitch)
        db.commit()
        db.refresh(pitch)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create lead pitch record",
        ) from exc

    enrich_company_task.delay(str(company.id))
    return pitch


@router.get("/pitches/{pitch_id}", response_model=LeadPitchResponse)
def get_lead_pitch(pitch_id: uuid.UUID, db: Session = Depends(get_db)) -> LeadPitch:
    """Poll the status/result of a previously requested lead pitch."""
    pitch = db.get(LeadPitch, pitch_id)
    if pitch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead pitch not found")
    return pitch
