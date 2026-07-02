"""Authentication: bcrypt password hashing, JWT issuance, and user resolution."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")

# One generic 401 for every credential failure (missing/expired/forged token,
# unknown user): distinct messages would let an attacker probe which part of
# a credential is wrong.
CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt (salted, adaptive work factor)."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time comparison of a plaintext password against its bcrypt hash."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        logger.warning("Malformed password hash encountered during verification")
        return False


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Return the user if the email/password pair is valid, else None."""
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is None:
        # Burn a bcrypt verification anyway so response timing does not
        # reveal whether the email exists (user-enumeration defense).
        pwd_context.dummy_verify()
        return None
    if not verify_password(password, user.hashed_password):
        return None
    if not user.is_active:
        return None
    return user


def create_access_token(user_id: uuid.UUID) -> str:
    """Issue a signed, short-lived JWT whose subject is the user's ID."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID:
    """Verify signature/expiry and return the user ID; raise 401 on any failure."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise CREDENTIALS_EXCEPTION

    if payload.get("type") != "access":
        raise CREDENTIALS_EXCEPTION

    try:
        return uuid.UUID(payload["sub"])
    except (ValueError, TypeError):
        raise CREDENTIALS_EXCEPTION


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """FastAPI dependency: resolve the Bearer token to an active User row."""
    user_id = decode_access_token(token)
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise CREDENTIALS_EXCEPTION
    return user
