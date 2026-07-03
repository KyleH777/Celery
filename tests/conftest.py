"""Shared fixtures: in-memory SQLite database, test client, and auth helpers.

Environment variables and the database engine are configured BEFORE any
`app.*` import, because `app.config.Settings` reads the environment at import
time and `app.main` binds the engine when the module loads.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-do-not-use-in-production")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_fake")
os.environ.setdefault("STRIPE_PRICE_ID", "price_fake")
os.environ.setdefault("MONTHLY_LEAD_CREDITS", "3")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.database as database

_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
database.engine = _engine
database.SessionLocal.configure(bind=_engine)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import SubscriptionStatus, User  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    """Fresh schema per test; rate limiting off unless a test opts in."""
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    main_module.limiter.enabled = False
    yield
    main_module.limiter.enabled = False


@pytest.fixture()
def client() -> TestClient:
    return TestClient(main_module.app, raise_server_exceptions=False)


@pytest.fixture()
def db_session():
    session = database.SessionLocal()
    yield session
    session.close()


def register_and_login(client: TestClient, email: str = "user@example.com") -> dict[str, str]:
    client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    response = client.post(
        "/api/v1/auth/token", data={"username": email, "password": "password123"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def entitle(email: str = "user@example.com", credits: int = 100) -> None:
    """Grant an active subscription and credits directly in the database."""
    session = database.SessionLocal()
    user = session.query(User).filter(User.email == email).first()
    user.subscription_status = SubscriptionStatus.ACTIVE
    user.lead_credits_remaining = credits
    session.commit()
    session.close()


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    """A registered, subscribed user with plenty of credits."""
    headers = register_and_login(client)
    entitle()
    return headers
