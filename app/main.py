"""FastAPI application entrypoint."""

from fastapi import FastAPI

from app.api.routes import router
from app.config import settings
from app.database import Base, engine

# NOTE: for production deployments, replace this with Alembic migrations.
Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.PROJECT_NAME)

app.include_router(router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
