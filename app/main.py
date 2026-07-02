"""FastAPI application entrypoint: routing, auth, rate limiting, CORS, error handling."""

import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import authenticate_user, create_access_token, get_current_user, hash_password
from app.config import settings
from app.database import Base, engine, get_db
from app.middleware import ENRICH_RATE_LIMIT, limiter, rate_limit_handler
from app.models import Company, LeadPitch, PitchStatus, User
from app.schemas import (
    DomainInput,
    LeadPitchResponse,
    PitchQueuedResponse,
    Token,
    UserCreate,
    UserResponse,
)
from app.worker import enrich_company_task

logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "Asynchronous B2B lead enrichment engine: submit a company domain, "
        "and a background pipeline scrapes the site, analyzes it with an LLM, "
        "and drafts a personalized cold outreach pitch. All lead data is "
        "isolated per authenticated user."
    ),
    version="2.0.0",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

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
        content={"detail": "Service temporarily unavailable. Please retry shortly."},
    )


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal error occurred."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected internal error occurred."},
    )


@app.post(
    "/api/v1/auth/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
    summary="Create a new user account",
)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    """Register with an email and password (min 8 characters).

    The password is bcrypt-hashed before storage; plaintext never touches
    the database or logs.
    """
    user = User(email=payload.email, hashed_password=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )
    db.refresh(user)
    return user


@app.post(
    "/api/v1/auth/token",
    response_model=Token,
    tags=["auth"],
    summary="Exchange credentials for a JWT access token",
)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> Token:
    """OAuth2 password flow: send `username` (your email) and `password` as
    form data; receive a Bearer token valid for a limited time.
    """
    user = authenticate_user(db, form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Token(access_token=create_access_token(user.id))


@app.post(
    "/api/v1/pitches/enrich",
    response_model=PitchQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["pitches"],
    summary="Queue enrichment and pitch generation for a domain",
)
@limiter.limit(ENRICH_RATE_LIMIT)
def enrich_domain(
    request: Request,
    payload: DomainInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PitchQueuedResponse:
    """Submit a company domain for enrichment and pitch generation.

    Requires a Bearer token. Limited to **5 requests per minute per user**
    (this endpoint fans out to scraping and LLM calls, so the limit protects
    both the service and your API spend). Companies and pitches are scoped
    to your account; the same domain submitted by another user is a fully
    separate record.

    Returns **202 Accepted** with the pitch ID to poll via
    `GET /api/v1/pitches/{pitch_id}`.
    """
    company = (
        db.query(Company)
        .filter(Company.user_id == current_user.id, Company.domain == payload.domain)
        .first()
    )

    if company is None:
        company = Company(user_id=current_user.id, domain=payload.domain)
        db.add(company)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            company = (
                db.query(Company)
                .filter(Company.user_id == current_user.id, Company.domain == payload.domain)
                .first()
            )
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
                LeadPitch.user_id == current_user.id,
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

    pitch = LeadPitch(company_id=company.id, user_id=current_user.id, status=PitchStatus.PENDING)
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
    responses={404: {"description": "No pitch with this ID exists in your account"}},
)
def get_pitch(
    pitch_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LeadPitch:
    """Fetch the current state of one of **your** pitch generation jobs.

    Tenant isolation: the lookup is always filtered by your user ID, so a
    pitch belonging to another account returns the same 404 as a pitch that
    does not exist — IDs cannot be probed across tenants.
    """
    pitch = (
        db.query(LeadPitch)
        .filter(LeadPitch.id == pitch_id, LeadPitch.user_id == current_user.id)
        .first()
    )
    if pitch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead pitch not found.")
    return pitch


@app.get("/health", tags=["health"], summary="Liveness probe")
def health_check() -> dict[str, str]:
    """Report service liveness (does not check database or broker health)."""
    return {"status": "ok"}
