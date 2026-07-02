# 🚀 ProspectGPT: Distributed B2B Lead Enrichment & AI Outreach Engine

**Turn a bare company domain into a research-backed, personalized cold outreach pitch — in under a minute, at queue-scale.**

Sales teams burn hours manually researching prospects: skimming websites, guessing pain points, and drafting outreach that still reads like spam. ProspectGPT automates the entire pipeline. Submit a domain, and a distributed background system scrapes the company's website, extracts structured business intelligence (value proposition, target audience, industry, size, pain points) with an LLM, and drafts a Problem-Agitate-Solve outreach message that cites a *specific, verifiable detail* from the prospect's own site — the difference between a reply and a delete.

The result: research that took an SDR 15 minutes per lead now runs unattended as a horizontally scalable job queue, with every intermediate artifact persisted, auditable, and retryable.

---

## 🛠️ System Architecture

Fully asynchronous, queue-backed pipeline. The API never blocks on scraping or LLM latency — it acknowledges in milliseconds and hands the heavy lifting to workers.

```
┌──────────────────┐        POST /api/v1/pitches/enrich
│   Streamlit UI   │ ─────────────────────────────────────┐
│  (ProspectGPT    │                                       ▼
│   Dashboard)     │                            ┌──────────────────┐
│                  │   GET /api/v1/pitches/{id} │  FastAPI Backend │
│  2s polling loop │ ◄─────────────────────────►│  (Pydantic v2    │
└──────────────────┘        202 Accepted        │   validation)    │
                                                └────────┬─────────┘
                                                         │ enqueue task
                                                         ▼
                                                ┌──────────────────┐
                                                │  Redis (broker)  │
                                                └────────┬─────────┘
                                                         │ consume
                                                         ▼
                        ┌────────────────────────────────────────────┐
                        │              Celery Worker                 │
                        │                                            │
                        │  1. Scraping Layer (httpx + BeautifulSoup) │
                        │     UA rotation · retries · tag stripping  │
                        │                     │                      │
                        │                     ▼                      │
                        │  2. AI Service (OpenAI Structured Output)  │
                        │     Step A: CompanyAnalysis extraction     │
                        │     Step B: PAS pitch generation           │
                        └────────────────────┬───────────────────────┘
                                             │ persist results
                                             ▼
                                  ┌──────────────────────┐
                                  │      PostgreSQL      │
                                  │  companies · pitches │
                                  │  analysis (JSON)     │
                                  └──────────────────────┘
```

**Data flow:** the UI submits a domain → FastAPI normalizes it, dedupes against in-flight jobs, writes a `pending` pitch row, and enqueues `enrich_company_task` → a Celery worker scrapes and cleans the site, runs a two-step structured-output LLM chain, and transitions the pitch through `processing → completed` (or `failed`) → the UI polls until terminal state and renders the full analysis.

---

## ⚡ Core Engineering Challenges & Solutions

### 💰 Token & Cost Optimization

LLM spend is the dominant marginal cost in any AI SaaS — unmanaged, it silently destroys unit economics.

- **Aggressive HTML reduction before the model ever sees a byte.** The scraper decomposes `script`, `style`, `nav`, `footer`, `header`, `aside`, `form`, `svg`, and `iframe` nodes, then extracts text from semantic content tags only (`h1`–`h3`, `p`). A typical marketing page shrinks from ~500KB of markup to a few KB of signal-dense text.
- **Lightweight-model-first strategy.** Both pipeline steps run on `gpt-4o-mini` — an order of magnitude cheaper than frontier models — because structured extraction over clean text doesn't need frontier reasoning. The model is a swappable constant, so premium tiers can route to stronger models without code changes.
- **Hard input budgets with graceful degradation.** Scraped text is capped at 48K characters at the service boundary; on `context_length_exceeded` or output-truncation errors, the prompt shrinks in 12K-character steps down to a 4K floor before the job is declared permanently failed — no unbounded retry loops billing the API.
- **Scrape-once semantics.** Raw text persists to `companies.raw_scraped_text`; re-pitching a known company skips the network entirely. Duplicate in-flight requests for the same domain return the existing job instead of enqueueing redundant scrape + LLM work.

### 🎯 Deterministic AI via Structured Outputs

Free-text LLM responses are a production liability: one malformed JSON blob and your parser throws at 3 AM.

- **Schema-enforced generation, not post-hoc parsing.** Every LLM call goes through `beta.chat.completions.parse` with strict Pydantic v2 models (`CompanyAnalysis`, `FinalOutreach`). The OpenAI API constrains decoding to the schema — the classic "model wrapped the JSON in markdown fences" failure mode is eliminated at the protocol level, not patched with regex.
- **Validation as a contract.** Field constraints (e.g., `pain_points` bounded to 2–3 items) are enforced by Pydantic on deserialization, so downstream code never defends against shape drift. Refusals and null parses raise typed exceptions instead of propagating `None`.
- **Two-step agent chain over one mega-prompt.** Step 1 extracts grounded facts (value prop, audience, industry, size, pain points); Step 2 consumes those facts plus the raw text to draft the pitch. Decomposition keeps each prompt focused, makes the intermediate analysis independently persistable and auditable, and lets the pitch prompt enforce its own constraint: *reference a specific detail from the website to prove a human-grade read.*

### 📈 Scalable Async Processing

A synchronous API would need to hold a connection open through a 10-second scrape (with up to 3 network retries) plus two LLM round trips — under modest concurrency that exhausts the server's worker pool, and every deploy or timeout silently kills in-flight jobs.

- **202-Accepted job pattern.** The API's only synchronous work is validation and two indexed inserts; it returns a pollable `pitch_id` immediately. Long-running work lives in Celery workers that scale horizontally (`docker compose up --scale celery_worker=N`) without touching the API tier.
- **Error taxonomy drives retry policy.** The AI service classifies failures as *retryable* (rate limits, connection errors, 5xx) or *permanent* (refusals, invalid requests). Retryable errors reset pitches to `pending` and re-enqueue with Celery's retry machinery (max 3 attempts); exhausted retries and permanent errors mark pitches `failed` — jobs always reach a terminal state, never a zombie limbo.
- **Explicit state machine.** `pending → processing → completed | failed` is persisted per pitch, so the UI, ops dashboards, and billing all read one source of truth. Workers use `pool_pre_ping` sessions, per-task session lifecycle, and rollback-on-exception so a dropped Postgres connection degrades to a retry, not a crash.
- **Defensive scraping.** Rotating browser User-Agents, 10-second timeouts, three-attempt retry with error-type-aware handling (timeouts retry; 4xx fail fast), and empty-site detection — a dead domain produces a clean `failed` status, never a hung worker.

---

## 💻 Tech Stack

| Layer | Technology | Role |
|---|---|---|
| **Backend** | FastAPI · Pydantic v2 · SQLAlchemy 2.0 (typed ORM) · Uvicorn | REST API, validation, persistence |
| **Frontend** | Streamlit | Polling dashboard with real-time job status |
| **AI / Data** | OpenAI Structured Outputs (`gpt-4o-mini`) · httpx · BeautifulSoup4 · PostgreSQL 16 | Extraction chain, scraping, storage |
| **MLOps / DevOps** | Celery 5 · Redis 7 · Docker Compose (5 services) · healthcheck-gated startup | Distributed queue, orchestration |

---

## ⚙️ Quick Start Guide

Prerequisites: Docker + Docker Compose, and an OpenAI API key.

```bash
# 1. Clone the repository
git clone https://github.com/KyleH777/Celery.git
cd Celery

# 2. Configure secrets (compose reads .env automatically)
echo "OPENAI_API_KEY=sk-your-key-here" > .env

# 3. Build and launch the entire 5-container ecosystem
docker compose up --build
```

That single command brings up PostgreSQL (with persistent volume), Redis, the FastAPI backend (hot-reload enabled), the Celery worker, and the dashboard — networked on a shared bridge and gated on database health checks.

| Endpoint | URL |
|---|---|
| 🎯 ProspectGPT Dashboard | http://localhost:8501 |
| 📚 Interactive API Docs (Swagger) | http://localhost:8000/docs |
| ❤️ API Health Check | http://localhost:8000/health |

**Try it:** open the dashboard, paste a domain like `stripe.com`, click **Analyze Company**, and watch the pipeline stream from `pending` to a fully personalized pitch.

```bash
# Or drive the API directly
curl -X POST http://localhost:8000/api/v1/pitches/enrich \
     -H "Content-Type: application/json" \
     -d '{"domain": "stripe.com"}'

curl http://localhost:8000/api/v1/pitches/<pitch_id>

# Scale workers independently of the API tier
docker compose up --build --scale celery_worker=4

# Tear down (add -v to wipe the database volume)
docker compose down
```

<details>
<summary><strong>Running without Docker (manual setup)</strong></summary>

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DATABASE_URL, REDIS_URL, OPENAI_API_KEY

# Terminal 1 — API
uvicorn app.main:app --reload

# Terminal 2 — Worker
celery -A app.celery_app.celery_app worker --loglevel=info

# Terminal 3 — Dashboard
streamlit run app.py
```

Requires local PostgreSQL and Redis reachable via the URLs in `.env`.

</details>

---

## 📁 Project Structure

```
.
├── app.py                    # Streamlit frontend (ProspectGPT Dashboard)
├── app/
│   ├── main.py               # FastAPI entrypoint: routes, CORS, error handlers
│   ├── config.py             # Pydantic settings (env vars)
│   ├── database.py           # SQLAlchemy engine / session / Base
│   ├── models.py             # ORM models: Company, LeadPitch (+ status enum)
│   ├── schemas.py            # API request/response schemas
│   ├── celery_app.py         # Celery application instance
│   ├── worker.py             # Pipeline task: scrape → analyze → pitch
│   ├── scraper.py            # httpx + BeautifulSoup scraping utility
│   ├── ai_service.py         # Two-step OpenAI structured-output chain
│   └── schemas_ai.py         # Strict Pydantic models for LLM outputs
├── Dockerfile.backend        # Multi-stage python:3.11-slim (API + worker)
├── Dockerfile.frontend       # Lean Streamlit image
├── docker-compose.yml        # db · redis · backend · celery_worker · frontend
├── requirements.txt
├── requirements-frontend.txt
└── .env.example
```

## 🔌 API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/pitches/enrich` | Submit a domain; returns `202` with a pollable `pitch_id`. Dedupes in-flight jobs per domain. |
| `GET` | `/api/v1/pitches/{pitch_id}` | Job status; on `completed`, returns the structured analysis + personalized pitch. |
| `GET` | `/health` | Liveness probe. |

Uniform JSON error envelopes throughout: `404` for unknown pitches, `422` for invalid domains, `503` when the database is unreachable, `500` for unexpected failures — no stack traces cross the API boundary.
