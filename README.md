# Lead Enrichment & Personalization Engine

Asynchronous B2B lead enrichment and AI-personalized pitch generation backend.

## Stack

FastAPI · Pydantic v2 · PostgreSQL (SQLAlchemy 2.0) · Celery · Redis · OpenAI

## Project Structure

```
.
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app entrypoint
│   ├── config.py         # Pydantic settings (env vars)
│   ├── database.py       # SQLAlchemy engine / session / Base
│   ├── models.py         # ORM models: Company, LeadPitch
│   ├── schemas.py        # Pydantic request/response schemas
│   ├── celery_app.py     # Celery application instance
│   ├── tasks.py          # Background tasks (pitch generation)
│   └── api/
│       ├── __init__.py
│       └── routes.py     # API endpoints
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in DATABASE_URL, REDIS_URL, OPENAI_API_KEY
```

## Running

```bash
# API server
uvicorn app.main:app --reload

# Celery worker (in a separate process)
celery -A app.celery_app.celery_app worker --loglevel=info
```

Redis must be running locally (or reachable via `REDIS_URL`), and PostgreSQL must be reachable via `DATABASE_URL`.

## API

- `POST /api/v1/companies` — create a company record
- `POST /api/v1/companies/{company_id}/pitches` — queue pitch generation
- `GET /api/v1/pitches/{pitch_id}` — poll pitch status/result
- `GET /health` — health check
