# Lead Enrichment & Personalization Engine

Asynchronous B2B lead enrichment and AI-personalized pitch generation backend.

## Stack

FastAPI · Pydantic v2 · PostgreSQL (SQLAlchemy 2.0) · Celery · Redis · OpenAI

## Project Structure

```
.
├── app.py                # Streamlit frontend (ProspectGPT Dashboard)
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI entrypoint: routes, CORS, error handlers
│   ├── config.py         # Pydantic settings (env vars)
│   ├── database.py       # SQLAlchemy engine / session / Base
│   ├── models.py         # ORM models: Company, LeadPitch
│   ├── schemas.py        # Pydantic request/response schemas
│   ├── celery_app.py     # Celery application instance
│   ├── worker.py         # Background task: scrape + AI pitch pipeline
│   ├── scraper.py        # httpx + BeautifulSoup scraping utility
│   ├── ai_service.py     # OpenAI structured-output analysis + PAS pitch
│   └── schemas_ai.py     # Pydantic models for LLM structured outputs
├── Dockerfile.backend    # API + worker image (multi-stage, python:3.11-slim)
├── Dockerfile.frontend   # Streamlit dashboard image
├── docker-compose.yml    # Full-stack orchestration (db, redis, api, worker, ui)
├── requirements.txt
├── requirements-frontend.txt
├── .dockerignore
├── .env.example
├── .gitignore
└── README.md
```

## Quick Start with Docker (recommended)

The entire stack — PostgreSQL, Redis, FastAPI backend, Celery worker, and the
Streamlit dashboard — is orchestrated with Docker Compose.

```bash
# 1. Provide your OpenAI key (compose reads .env automatically)
echo "OPENAI_API_KEY=sk-your-key" > .env

# 2. Build and launch all five services
docker compose up --build

# 3. Open the apps
#    Dashboard:    http://localhost:8501
#    API docs:     http://localhost:8000/docs
#    Health check: http://localhost:8000/health

# Stop everything (add -v to also wipe the Postgres volume)
docker compose down
```

| Service         | Image / Build        | Port | Purpose                          |
|-----------------|----------------------|------|----------------------------------|
| `db`            | `postgres:16-alpine` | 5432 | Primary datastore                |
| `redis`         | `redis:7-alpine`     | 6379 | Celery broker & result backend   |
| `backend`       | `Dockerfile.backend` | 8000 | FastAPI API (hot-reload enabled) |
| `celery_worker` | `Dockerfile.backend` | —    | Scrape + AI pitch pipeline       |
| `frontend`      | `Dockerfile.frontend`| 8501 | ProspectGPT Streamlit dashboard  |

The backend and worker mount `./app` into the container, so code changes
hot-reload without rebuilding. All services share the `lead_net` bridge
network and address each other by service name (`db`, `redis`, `backend`).

## Manual Setup (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in DATABASE_URL, REDIS_URL, OPENAI_API_KEY
```

### Running

```bash
# API server
uvicorn app.main:app --reload

# Celery worker (in a separate process)
celery -A app.celery_app.celery_app worker --loglevel=info

# Streamlit dashboard (in a separate process; set API_BASE_URL if the API is remote)
streamlit run app.py
```

Redis must be running locally (or reachable via `REDIS_URL`), and PostgreSQL must be reachable via `DATABASE_URL`.

## API

- `POST /api/v1/pitches/enrich` — submit a domain; queues scrape → analysis → pitch and returns the pitch ID (202)
- `GET /api/v1/pitches/{pitch_id}` — poll status; returns analysis + personalized pitch when `completed`
- `GET /health` — liveness probe

Interactive Swagger docs at `/docs` once the server is running.
