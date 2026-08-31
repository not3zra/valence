<div align="center">

# Valence

### Order Intake & Fulfillment Agent

**A single AI agent that turns orders arriving over WhatsApp or recorded phone calls — in any language — into structured orders with automated approval, dispatch lists, and Tally billing.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Google ADK](https://img.shields.io/badge/Google_ADK-2.6.3-4285F4.svg)](https://cloud.google.com/agent-engine)
[![Gemini](https://img.shields.io/badge/Gemini-3.5_Flash-FF6D00.svg)](https://ai.google.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![Cloud Run](https://img.shields.io/badge/Cloud_Run-Deployed-4285F4.svg)](https://cloud.google.com/run)
[![Firestore](https://img.shields.io/badge/Firestore-Active-FFCA28.svg)](https://firebase.google.com/docs/firestore)
[![Tests](https://img.shields.io/badge/tests-393_passing-16a34a.svg)]()
[![License](https://img.shields.io/badge/license-MIT-gray.svg)]()

</div>

---

## Overview

Valence is a production-grade order intake system built for chemical distributors. It replaces manual WhatsApp/phone order handling with an AI agent that understands orders in **any language or format**, auto-approves clean orders, escalates exceptions to human reviewers, generates dispatch loading lists, and produces Tally-compatible billing vouchers — all from a single Cloud Run service.

### Key Capabilities

| Channel | Input | Processing |
|---------|-------|------------|
| **WhatsApp Text** | Free-text messages in any language | Gemini extracts structured order |
| **Phone Call** | Recorded sales calls (.wav/.ogg) | Audio understood in single model call |

### What It Does

1. **Intake** — Receives orders via WhatsApp text or company-recorded phone calls
2. **Extraction** — Gemini 3.5 Flash understands any language/format and extracts structured items
3. **Decision** — Auto-escalates unknown customers, low confidence, anomalies, or missing fields
4. **Approval** — Escalated orders notify allowlisted approvers via WhatsApp; approvers confirm via WhatsApp reply or the review web dashboard
5. **Dispatch** — Generates per-route Loading Lists for warehouse teams
6. **Billing** — Produces GST-compliant Tally voucher XML for import

---

## Architecture

```mermaid
graph TB
    subgraph "Intake Channels"
        WA[WhatsApp Text]
        VC[Phone Call Recording]
        API[REST API]
    end

    subgraph "Cloud Run Service"
        WH[Webhook Handler<br/>FastAPI]
        AG[ADK Agent<br/>Gemini 3.5 Flash]
        CORE[Order Processing Core<br/>Decision Engine]
        TOOLS[Agent Tools<br/>process_order · approve_order<br/>prepare_voucher · render_loading_list]
    end

    subgraph "Data Layer"
        FS[(Firestore<br/>Sessions · Orders · Config)]
        GCS[(Cloud Storage<br/>Tally Vouchers)]
    end

    subgraph "Outputs"
        WA2[WhatsApp Replies]
        REVIEW[Review Web View<br/>/review]
        LOADING[Loading List<br/>/loading]
        TALLY[Tally Import<br/>XML Voucher]
    end

    WA -->|Webhook| WH
    VC -->|Audio bytes| WH
    API -->|Roundtrip probe| WH

    WH --> AG
    AG --> CORE
    AG --> TOOLS
    CORE --> FS
    TOOLS --> GCS

    AG -->|Reply| WA2
    TOOLS --> REVIEW
    TOOLS --> LOADING
    TOOLS --> TALLY

    style AG fill:#4285F4,color:#fff
    style CORE fill:#0F9D58,color:#fff
    style FS fill:#FFCA28,color:#000
    style GCS fill:#4285F4,color:#fff
```

### Order Lifecycle

```mermaid
stateDiagram-v2
    [*] --> PendingReview: Order Created
    PendingReview --> Approved: Auto-approved (clean)
    PendingReview --> Escalated: Exception detected
    PendingReview --> Rejected: Rejected by approver
    PendingReview --> Clarify: Missing fields (WhatsApp only)
    Clarify --> PendingReview: Customer replies
    Escalated --> Approved: Approver confirms
    Escalated --> Rejected: Approver rejects
    Approved --> Dispatched: Loading List generated
    Dispatched --> Billed: Tally voucher created
    Billed --> [*]
    Rejected --> [*]
```

---

## Screenshots

### WhatsApp Order Flow
<!-- Replace with actual screenshot -->
![WhatsApp Order Flow](screenshots/whatsapp-order-flow.png)

*Customer sends an order in Hindi via WhatsApp; agent extracts items, estimates value, and confirms.*

### Review Dashboard
<!-- Replace with actual screenshot -->
![Review Dashboard](screenshots/review-dashboard.png)

*Passcode-gated approval queue showing pending escalations with reason badges, live stats, and quick actions.*

### Order Detail Page
<!-- Replace with actual screenshot -->
![Order Detail](screenshots/order-detail.png)

*Full order view with items, customer info, Order Event timeline, and approve/reject/edit actions.*

### Loading List
<!-- Replace with actual screenshot -->
![Loading List](screenshots/loading-list.png)

*Dispatch-facing view grouped by delivery route, with late add-ons section after cutoff.*

### Tally Voucher
<!-- Replace with actual screenshot -->
![Tally Voucher](screenshots/tally-voucher.png)

*GST-compliant sales invoice XML ready for Tally import.*

---

## Spin-Up Instructions

These instructions let you run the full project locally or deploy it to Google Cloud. The project is fully reproducible — all dependencies, seed data, and configuration are in the repo.

### Prerequisites

| Requirement | Version | Install |
|-------------|---------|---------|
| **Python** | 3.10+ | [python.org](https://www.python.org/downloads/) |
| **gcloud CLI** | Latest | [cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install) |
| **Firestore emulator** | — | `gcloud components install cloud-firestore-emulator` |
| **Git** | — | [git-scm.com](https://git-scm.com/) |

### Option A: Run Locally (Firestore Emulator + Vertex AI)

This is the fastest way to test the agent. The Firestore emulator runs in-memory locally; Gemini runs through Vertex AI on the shared GCP project.

#### Step 1: Clone and install dependencies

```bash
git clone https://github.com/not3zra/valence.git
cd valence

python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

pip install -e ".[dev]"
```

#### Step 2: Install the Firestore emulator

```bash
gcloud components install cloud-firestore-emulator
```

#### Step 3: Authenticate to Google Cloud

You need access to the `valence-505412` project for Vertex AI (Gemini model). Ask the project owner to add your Google account.

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project valence-505412
```

#### Step 4: Start the service

```bash
./scripts/run_local.sh
```

This script:
1. Starts the Firestore emulator on port 8686
2. Seeds Firestore with customers, products, routes, approvers, and thresholds
3. Starts uvicorn on port 8080

The service is now live at `http://localhost:8080`.

#### Step 5: Test it

**Send a test order via the API:**

```bash
curl -s localhost:8080/api/roundtrip \
  -H 'authorization: Bearer local-dev-roundtrip-token' \
  -H 'content-type: application/json' \
  -d '{"sender_id": "+919812345001", "message": "Namaste, 2 drums sulfuric acid chahiye"}'
```

**Open the review dashboard:**

Navigate to `http://localhost:8080/review` in your browser. Enter the passcode: `valence-demo`

**Open the loading list:**

Navigate to `http://localhost:8080/loading` in your browser.

### Option B: Run with Memory Store (No GCP Access)

If you don't have GCP project access, use the in-memory store. The agent will still work but won't persist data across restarts.

```bash
# Set environment variables
export SESSION_SERVICE=memory
export ROUNDTRIP_TOKEN=local-dev-roundtrip-token
export WEB_PASSCODE=valence-demo
export WEB_PASSCODE_SALT=local-dev-salt

# Start the service directly
uvicorn src.main:app --host 0.0.0.0 --port 8080
```

### Option C: Deploy to Google Cloud Run

#### Step 1: Set up secrets

```bash
# Set your project
gcloud config set project valence-505412

# Create secrets (replace with your actual values)
echo -n "your-meta-app-secret" | gcloud secrets create META_APP_SECRET --data-file=-
echo -n "your-meta-verify-token" | gcloud secrets create META_VERIFY_TOKEN --data-file=-
echo -n "your-meta-access-token" | gcloud secrets create META_ACCESS_TOKEN --data-file=-
echo -n "your-meta-phone-number-id" | gcloud secrets create META_PHONE_NUMBER_ID --data-file=-
echo -n "your-roundtrip-token" | gcloud secrets create ROUNDTRIP_TOKEN --data-file=-
echo -n "your-voice-ingest-token" | gcloud secrets create VOICE_INGEST_TOKEN --data-file=-
echo -n "your-web-passcode" | gcloud secrets create WEB_PASSCODE --data-file=-
echo -n "your-web-passcode-salt" | gcloud secrets create WEB_PASSCODE_SALT --data-file=-
echo -n "your-cutoff-secret" | gcloud secrets create CUTOFF_SECRET --data-file=-
```

#### Step 2: Deploy

```bash
gcloud run deploy valence \
  --source . \
  --region=us-central1 \
  --project=valence-505412 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=valence-505412,GOOGLE_CLOUD_LOCATION=asia-southeast1,VOUCHER_BUCKET=valence-505412-vouchers" \
  --set-secrets="META_APP_SECRET=META_APP_SECRET:latest,META_VERIFY_TOKEN=META_VERIFY_TOKEN:latest,META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest,META_PHONE_NUMBER_ID=META_PHONE_NUMBER_ID:latest,ROUNDTRIP_TOKEN=ROUNDTRIP_TOKEN:latest,VOICE_INGEST_TOKEN=VOICE_INGEST_TOKEN:latest,WEB_PASSCODE=WEB_PASSCODE:latest,WEB_PASSCODE_SALT=WEB_PASSCODE_SALT:latest"
```

#### Step 3: Redirect traffic

```bash
# Get the latest revision name
REVISION=$(gcloud run revisions list --service=valence --region=us-central1 --project=valence-505412 --limit=1 --format="value(metadata.name)")

# Route all traffic to it
gcloud run services update-traffic valence \
  --region=us-central1 \
  --project=valence-505412 \
  --to-revisions=$REVISION=100
```

### Option D: Full Provisioning (One-Shot)

For a fresh GCP project, the provision script creates everything:

```bash
# Create .env.provision with your values
cat > .env.provision << 'EOF'
PROJECT_ID=valence-505412
META_APP_SECRET=your-secret
META_VERIFY_TOKEN=your-token
META_ACCESS_TOKEN=your-token
META_PHONE_NUMBER_ID=your-id
ROUNDTRIP_TOKEN=your-token
VOICE_INGEST_TOKEN=your-token
WEB_PASSCODE=your-passcode
WEB_PASSCODE_SALT=your-salt
CUTOFF_SECRET=your-secret
EOF

source .env.provision
./infra/provision.sh
```

This provisions: Cloud Run, Firestore, Cloud Storage, Pub/Sub, Cloud Scheduler, IAM, and Secret Manager.

### Run Tests

```bash
# Full test suite (393 tests)
pytest

# Core logic tests only
pytest tests/test_core.py

# Linting
ruff check .

# Type checking
mypy src
```

### Agent Evaluation

Run the eval harness against the real Gemini model:

```bash
# Via Vertex AI (recommended)
GOOGLE_GENAI_USE_VERTEXAI=true \
GOOGLE_CLOUD_PROJECT=valence-505412 \
GOOGLE_CLOUD_LOCATION=asia-southeast1 \
python scripts/eval_agent.py

# List available cases
python scripts/eval_agent.py --list

# Run specific category
python scripts/eval_agent.py --category safety
```

---

## Tech Stack

### Core Services

| Service | Purpose | Configuration |
|---------|---------|---------------|
| **Cloud Run** | Hosts the FastAPI service (webhooks, web views, API) | `us-central1`, auto-scales |
| **Firestore** | Durable sessions, orders, config, seed data | Native mode, `(default)` database |
| **Cloud Storage** | Tally voucher XML storage | `valence-<project>-vouchers` |
| **Vertex AI** | Serves Gemini 3.5 Flash model | `asia-southeast1` region |
| **Secret Manager** | WhatsApp credentials, tokens, passcodes | 9 secrets bound to service |
| **Pub/Sub** | Cutoff trigger chain | `valence-cutoff` topic |
| **Cloud Scheduler** | Daily cutoff job (17:31 IST) | Triggers Loading List render |
| **Cloud Build** | CI/CD — auto-deploy on push to `main` | Docker build + deploy |

### AI & Agent Framework

| Component | Version | Role |
|-----------|---------|------|
| **Google ADK** | `2.6.3` | Agent runtime, tool execution, session management |
| **Gemini 3.5 Flash** | `gemini-3.5-flash` | Multimodal understanding (text + audio in one call) |
| **Vertex AI** | Production | Model serving (not free-tier API key) |

### Application Stack

| Component | Technology |
|-----------|------------|
| **Web Framework** | FastAPI (async) |
| **Language** | Python 3.10+ |
| **Session Persistence** | ADK `FirestoreSessionService` |
| **WhatsApp Integration** | Meta Cloud API (Graph API v20.0) |
| **GST/Tally** | Custom voucher XML builder |
| **Testing** | pytest + pytest-asyncio (393 tests) |
| **Linting** | ruff + mypy |
| **Containerization** | Docker (Python 3.12-slim) |

---

## Project Structure

```
valence/
├── src/
│   ├── agent.py           # ADK agent definition + session management
│   ├── agent_exec.py      # TurnExecutor — persistent event loop for ADK
│   ├── config.py          # Environment settings
│   ├── core.py            # Order Processing Core (decision engine)
│   ├── core_tool.py       # Agent tools (process_order, approve_order, etc.)
│   ├── orders.py          # Order domain model + status state machine
│   ├── store.py           # Firestore adapter + in-memory test double
│   ├── web.py             # FastAPI web layer (webhooks, views, API)
│   ├── review.py          # Review dashboard HTML renderer
│   ├── loading.py         # Loading List renderer
│   ├── voucher.py         # Tally voucher XML builder + Cloud Storage
│   ├── approval.py        # Human approval notification (WhatsApp)
│   ├── dispatch.py        # Late order notification (WhatsApp)
│   ├── media.py           # Media handling (photo/audio)
│   ├── meta_whatsapp.py   # Meta Cloud API adapter
│   ├── ratelimit.py       # Per-sender rate limiter
│   ├── seed_data.py       # Canonical seed data (customers, products, routes)
│   ├── seed_firestore.py  # Firestore seeder
│   ├── ui.py              # Shared design system (CSS tokens, components)
│   └── main.py            # Production entry point (uvicorn)
├── tests/                 # 393 tests (pytest + pytest-asyncio)
├── scripts/
│   ├── run_local.sh       # Local dev with Firestore emulator
│   ├── eval_agent.py      # Agent eval harness (24 cases)
│   ├── feed_order.py      # Drive core without channel
│   ├── feed_voice.py      # Batch voice ingestion
│   ├── tally_sync.py      # Local Tally auto-sync script
│   └── smoke_*.py         # Smoke tests
├── infra/
│   └── provision.sh       # One-shot GCP provisioning
├── docs/
│   └── adr/               # Architectural Decision Records
├── Dockerfile
├── cloudbuild.yaml        # CI/CD pipeline
└── pyproject.toml
```

---

## API Reference

### `POST /api/roundtrip`

Debug probe — drive the agent directly without WhatsApp.

```bash
curl -X POST http://localhost:8080/api/roundtrip \
  -H "Authorization: Bearer local-dev-roundtrip-token" \
  -H "Content-Type: application/json" \
  -d '{"sender_id": "+919812345001", "message": "2 drums sulfuric acid"}'
```

### `POST /api/voice/ingest`

Company-recorded call ingestion.

```bash
curl -X POST http://localhost:8080/api/voice/ingest \
  -H "Authorization: Bearer local-dev-ingest-token" \
  -H "Content-Type: application/json" \
  -d '{"caller": "+919812345001", "audio_base64": "<base64>", "mime_type": "audio/wav"}'
```

### `GET /api/vouchers`

List all prepared vouchers (passcode-gated).

### `POST /review/orders/{id}/push-to-tally`

Push a prepared voucher directly to local Tally (requires `TALLY_PUSH_URL`).

---

## Design Decisions

### ADR-0001: Unified Understanding Layer

One ADK Agent receives messages from every channel (WhatsApp text, voice) and runs Gemini to extract structured orders. No separate translate → transcribe → parse pipeline — a single model call keeps one source of truth.

### ADR-0002: Graduated Approval

Clean orders auto-approve. Exceptions escalate as hard blocks (unknown customer, low confidence, anomaly, missing field). Thresholds are Firestore-configurable, not hardcoded.

### ADR-0003: Tally Pre-Seeded Masters

Voucher generation references only pre-seeded, mapped Tally masters (party ledgers, stock items, GST ledgers). Any unmapped master blocks generation with an explicit error.

### ADR-0004: Call Orders Flagged, Not Clarified

Voice orders with missing fields escalate to human review immediately — no clarifying questions over recorded calls.

---

## Contributing

1. Create a feature branch from `main`
2. Make changes with tests
3. Run `pytest && ruff check . && mypy src`
4. Open a PR — CI builds and deploys a preview revision

---

## License

MIT
