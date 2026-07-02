"""Application configuration loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized, typed application settings.

    Values are read from environment variables first and fall back to a
    local `.env` file (see `.env.example`) when present.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "Lead Enrichment Engine"
    API_V1_PREFIX: str = "/api/v1"

    # Core infrastructure
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"
    OPENAI_API_KEY: str

    # Auth / JWT — no default: the app must fail fast rather than boot
    # with a guessable signing key. Generate one with `openssl rand -hex 32`.
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Celery defaults to REDIS_URL unless explicitly overridden.
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None

    def model_post_init(self, __context: object) -> None:
        if not self.CELERY_BROKER_URL:
            self.CELERY_BROKER_URL = self.REDIS_URL
        if not self.CELERY_RESULT_BACKEND:
            self.CELERY_RESULT_BACKEND = self.REDIS_URL


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor so the .env file is only parsed once."""
    return Settings()


settings = get_settings()
