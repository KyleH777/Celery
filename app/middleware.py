"""Redis-backed API rate limiting via slowapi.

How the limiter interacts with Redis
------------------------------------
slowapi delegates counting to the `limits` library, pointed at our existing
Redis container through `storage_uri`. For every request it builds a key like

    LIMITER/user:<uuid>/api_v1_pitches_enrich/5/1/minute

and executes an atomic INCR on it; the first hit in a window also sets an
EXPIRE equal to the window length (60s for "5/minute"), so Redis evicts
counters automatically and the store never grows unbounded. Because the
counter lives in Redis rather than process memory, the limit holds globally
across every uvicorn worker and API replica — a client cannot bypass it by
having requests land on different instances.

If Redis is briefly unreachable, `in_memory_fallback_enabled` keeps limiting
per-process instead of failing open (no limiting at all) or closed (every
request 500s). Counters resume in Redis when it recovers.
"""

import logging

import jwt
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

ENRICH_RATE_LIMIT = "5/minute"


def user_or_ip_key(request: Request) -> str:
    """Rate-limit key: the authenticated user ID, falling back to client IP.

    The token signature IS verified here — otherwise an attacker could mint
    unsigned tokens with random subjects to get a fresh bucket per request.
    Invalid tokens simply fall through to the IP bucket; rejecting them is
    the auth dependency's job, not the limiter's.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            payload = jwt.decode(
                auth_header[7:],
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            subject = payload.get("sub")
            if subject:
                return f"user:{subject}"
        except jwt.InvalidTokenError:
            pass

    client_ip = request.client.host if request.client else "unknown"
    return f"ip:{client_ip}"


limiter = Limiter(
    key_func=user_or_ip_key,
    storage_uri=settings.REDIS_URL,
    strategy="fixed-window",
    in_memory_fallback_enabled=True,
)


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Uniform JSON 429 with Retry-After; never leaks limiter internals."""
    logger.warning(
        "Rate limit exceeded for key %s on %s %s",
        user_or_ip_key(request),
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded: maximum 5 enrichment requests per minute. Please retry shortly."},
        headers={"Retry-After": "60"},
    )
