# 🚀 ProspectGPT: Distributed B2B Lead Enrichment & AI Outreach Engine

[![CI](https://github.com/KyleH777/Celery/actions/workflows/ci.yml/badge.svg)](https://github.com/KyleH777/Celery/actions/workflows/ci.yml)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-Pydantic%20v2-009688.svg)
![Tests](https://img.shields.io/badge/tests-71%20passing-brightgreen.svg)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

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
echo "JWT_SECRET_KEY=$(openssl rand -hex 32)" >> .env

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

## 🧪 Testing & Quality

A 71-test pytest suite runs on every push via GitHub Actions, alongside ruff linting:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v          # full suite (in-memory SQLite; no external services needed)
ruff check .       # lint
```

| Suite | Covers |
|---|---|
| `test_auth.py` | Registration, login, forged/expired/garbage token rejection, bcrypt salting, enumeration-safe errors |
| `test_pitches.py` | Job queueing, domain normalization, in-flight dedupe, cross-tenant 404s, per-user rate-limit buckets |
| `test_billing.py` | Checkout (customer reuse), webhook signature rejection, event idempotency, credit provisioning/exhaustion/refund, past-due restriction |
| `test_ai_service.py` | Two-step chain wiring, retryable-vs-permanent error taxonomy, context-overflow shrinking, input capping |
| `test_worker.py` | Pipeline state machine, credit refund on failure, email dispatch, broker-failure isolation |
| `test_scraper.py` | Boilerplate stripping, 4xx fail-fast vs timeout retries, dead/empty site handling |

External integrations (OpenAI, Stripe, Resend, Redis) are mocked at the SDK
boundary, so the suite is deterministic, fast (~20s), and runs anywhere —
including CI — with zero credentials.

## 📁 Project Structure

```
.
├── app.py                    # Streamlit frontend (ProspectGPT Dashboard)
├── app/
│   ├── main.py               # FastAPI entrypoint: routes, CORS, error handlers
│   ├── config.py             # Pydantic settings (env vars)
│   ├── database.py           # SQLAlchemy engine / session / Base
│   ├── models.py             # ORM models: User, Company, LeadPitch
│   ├── schemas.py            # API request/response schemas
│   ├── auth.py               # bcrypt hashing, JWT issuance, current-user dependency
│   ├── middleware.py         # Redis-backed per-user rate limiting (slowapi)
│   ├── billing_service.py    # Stripe checkout, entitlements, credit accounting
│   ├── webhooks.py           # Idempotent, signature-verified Stripe webhooks
│   ├── notification_service.py  # Resend transactional email (batch completion)
│   ├── celery_app.py         # Celery application instance
│   ├── worker.py             # Pipeline task: scrape → analyze → pitch
│   ├── scraper.py            # httpx + BeautifulSoup scraping utility
│   ├── ai_service.py         # Two-step OpenAI structured-output chain
│   └── schemas_ai.py         # Strict Pydantic models for LLM outputs
├── tests/                    # 71-test pytest suite (auth, billing, pipeline, AI, scraper)
├── .github/workflows/ci.yml  # Lint + test on every push
├── Dockerfile.backend        # Multi-stage python:3.11-slim (API + worker)
├── Dockerfile.frontend       # Lean Streamlit image
├── docker-compose.yml        # db · redis · backend · celery_worker · frontend
├── pyproject.toml            # pytest + ruff configuration
├── requirements.txt
├── requirements-frontend.txt / requirements-dev.txt
└── .env.example
```

## 🔌 API Reference

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/auth/register` | — | Create an account (bcrypt-hashed password). |
| `POST` | `/api/v1/auth/token` | — | OAuth2 password flow; returns a JWT Bearer token. |
| `POST` | `/api/v1/pitches/enrich` | 🔒 JWT | Submit a domain; returns `202` with a pollable `pitch_id`. **Rate-limited to 5/min per user** (Redis-backed). Dedupes in-flight jobs per domain. |
| `GET` | `/api/v1/pitches/{pitch_id}` | 🔒 JWT | Job status; on `completed`, returns the structured analysis + personalized pitch. |
| `GET` | `/health` | — | Liveness probe. |

Uniform JSON error envelopes throughout: `401` for missing/invalid tokens, `404` for unknown pitches, `422` for invalid domains, `429` when rate-limited (with `Retry-After`), `503` when the database is unreachable, `500` for unexpected failures — no stack traces or internal details cross the API boundary.

## 🔐 Security Model

- **Authentication** — OAuth2 password flow issuing short-lived HS256 JWTs (PyJWT); passwords bcrypt-hashed via Passlib with per-hash salts. Login timing is equalized against user enumeration, and all credential failures return one generic 401.
- **Multi-tenant isolation** — every `Company` and `LeadPitch` row carries a `user_id` foreign key; all queries filter by the authenticated user, and cross-tenant IDs return the same 404 as nonexistent ones. Domains are unique *per tenant*, so two customers researching the same company never share records.
- **Rate limiting** — `slowapi` backed by the existing Redis container: atomic `INCR` + window `EXPIRE` per user key, enforced globally across all API replicas, with an in-memory fallback if Redis blips. The enrich endpoint (which fans out to scraping + LLM spend) is capped at 5 requests/minute per user.
- **Fail-fast secrets** — the app refuses to boot without `JWT_SECRET_KEY` (generate with `openssl rand -hex 32`).

## 💳 Billing & Notifications

- **Stripe subscriptions** — `POST /api/v1/billing/checkout` returns a hosted Checkout URL; signature-verified webhooks (`/api/v1/billing/webhook`) activate accounts, provision monthly lead credits on paid invoices, and mark accounts `past_due` on failed payments. Webhook processing is idempotent: each event ID commits atomically with the changes it caused, so Stripe redeliveries can never double-apply. Test locally with `stripe listen --forward-to localhost:8000/api/v1/billing/webhook`.
- **Credit metering** — one credit is reserved per queued enrichment via an atomic conditional UPDATE (race-safe under concurrency) and refunded automatically if the job terminally fails. `GET /api/v1/billing/me` reports status and remaining quota.
- **Email notifications (Resend)** — when a batch completes, a decoupled Celery task emails the owner a styled summary with a dashboard link (plus RFC 8058 List-Unsubscribe headers and preference-center placeholders). Delivery is strictly best-effort: an email failure is logged and can never roll back or crash the enrichment it reports on. Leave `RESEND_API_KEY` empty to disable.
