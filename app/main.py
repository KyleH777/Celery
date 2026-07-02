"""FastAPI application entrypoint: routing, CORS, and global error handling."""

import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, engine, get_db
from app.models import Company, LeadPitch, PitchStatus
from app.schemas import DomainInput, LeadPitchResponse, PitchQueuedResponse
from app.worker import enrich_company_task

logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "Asynchronous B2B lead enrichment engine: submit a company domain, "
        "and a background pipeline scrapes the site, analyzes it with an LLM, "
        "and drafts a personalized cold outreach pitch."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(OperationalError)
async def database_connection_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    logger.error("Database connection error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database temporarily unavailable. Please retry shortly."},
    )


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal database error occurred."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal error occurred."},
    )


@app.post(
    "/api/v1/pitches/enrich",
    response_model=PitchQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["pitches"],
    summary="Queue enrichment and pitch generation for a domain",
)
def enrich_domain(payload: DomainInput, db: Session = Depends(get_db)) -> PitchQueuedResponse:
    """Submit a company domain for enrichment and pitch generation.

    The domain is normalized (scheme, `www.`, and path are stripped). If the
    company is not yet known, a `Company` record is created. A `LeadPitch`
    record is created in `pending` status and the background pipeline
    (scrape → LLM analysis → PAS pitch) is triggered asynchronously.

    If a pitch for this domain is already `pending` or `processing`, that
    in-flight pitch is returned instead of queueing duplicate work.

    Returns **202 Accepted** with the pitch ID to poll via
    `GET /api/v1/pitches/{pitch_id}`.
    """
    company = db.query(Company).filter(Company.domain == payload.domain).first()

    if company is None:
        company = Company(domain=payload.domain)
        db.add(company)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            company = db.query(Company).filter(Company.domain == payload.domain).first()
            if company is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create company record.",
                )
        else:
            db.refresh(company)
    else:
        in_flight = (
            db.query(LeadPitch)
            .filter(
                LeadPitch.company_id == company.id,
                LeadPitch.status.in_([PitchStatus.PENDING, PitchStatus.PROCESSING]),
            )
            .first()
        )
        if in_flight is not None:
            return PitchQueuedResponse(
                pitch_id=in_flight.id,
                company_id=company.id,
                domain=company.domain,
                status=in_flight.status,
            )

    pitch = LeadPitch(company_id=company.id, status=PitchStatus.PENDING)
    db.add(pitch)
    db.commit()
    db.refresh(pitch)

    enrich_company_task.delay(str(company.id))

    return PitchQueuedResponse(
        pitch_id=pitch.id,
        company_id=company.id,
        domain=company.domain,
        status=pitch.status,
    )


@app.get(
    "/api/v1/pitches/{pitch_id}",
    response_model=LeadPitchResponse,
    tags=["pitches"],
    summary="Poll the status/result of a pitch generation job",
    responses={404: {"description": "No pitch exists with the given ID"}},
)
def get_pitch(pitch_id: uuid.UUID, db: Session = Depends(get_db)) -> LeadPitch:
    """Fetch the current state of a pitch generation job.

    - While the job is running, `status` is `pending` or `processing` and
      `generated_pitch` / `analysis` are `null`.
    - On success, `status` is `completed`, `generated_pitch` contains the
      personalized PAS outreach message, and `analysis` contains the
      extracted value proposition, target audience, and pain points.
    - If the pipeline exhausted retries or hit a permanent error,
      `status` is `failed`.
    """
    pitch = db.get(LeadPitch, pitch_id)
    if pitch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead pitch not found.")
    return pitch


@app.get("/health", tags=["health"], summary="Liveness probe")
def health_check() -> dict[str, str]:
    """Report service liveness (does not check database or broker health)."""
    return {"status": "ok"}
