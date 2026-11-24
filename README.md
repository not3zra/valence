# Valence

### Portable order intake & fulfillment agent

One ADK agent turns WhatsApp text, WhatsApp photos, and recorded phone calls — in any language — into structured orders, with human approval, per-route Loading Lists, and Tally billing vouchers. It runs as one FastAPI process against SQLite on a laptop, or through Docker Compose with Postgres. No cloud account, Firestore, or telephony provider is needed to start.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![Google ADK](https://img.shields.io/badge/Google_ADK-2.6.3-4285F4.svg)](https://google.github.io/adk-docs/)
[![MCP](https://img.shields.io/badge/MCP-streamable_HTTP-7C3AED.svg)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-gray.svg)]()

---

## What you get

- **Unified intake** — text and photos from WhatsApp plus company-recorded calls, understood by one model call in any language.
- **Graduated approvals** — clean orders auto-approve; unknown customers, low confidence, anomalies, and missing fields escalate to a human instead of shipping.
- **Agent Interface** — a versioned REST surface and an MCP endpoint at `/mcp` on the same app and port, sharing one authorization layer.
- **Operational artifacts** — price-free Loading Lists grouped by route, late add-ons after cutoff, and GST-compliant Tally voucher XML.
- **Portable persistence** — SQLite by default; Postgres through Compose; optional Firestore adapters behind the `[gcp]` extra.
- **Offline testability** — fake model providers and a local fake Tally let the real process be exercised without secrets.

## Requirements

| Requirement | Version | Needed for |
|-------------|---------|------------|
| Python | 3.12+ | All local commands |
| [uv](https://docs.astral.sh/uv/) | Any recent | Reproducible install from `uv.lock` |
| Docker with Compose | Any recent | The Postgres profile (optional) |
| Git | Any | Cloning the repository |
| Model credentials | — | Live intake; not needed for `MODEL_PROVIDER=fake` |

---

## Quickstart: local SQLite

These steps boot a clean Instance on SQLite and mirror the real-process end-to-end harness. Use `MODEL_PROVIDER=fake` for an offline walkthrough, or supply a Gemini key for real orders.

### 1. Install

```bash
git clone https://github.com/not3zra/valence.git
cd valence
uv sync --all-extras
```

`uv sync --all-extras` installs the pinned dependency set from `uv.lock`, including the dev tools and optional GCP adapters.

### 2. Migrate and bootstrap

```bash
export DATABASE_URL='sqlite+aiosqlite:///./valence.db'
uv run alembic upgrade head
uv run valence init \
  --business-name "Acme Chemicals" \
  --timezone "Asia/Kolkata"
```

`alembic upgrade head` applies the committed migrations. `valence init` then creates the Instance profile, seeds the sample catalogue when it is empty, and issues the first admin key. The plaintext key is printed once — copy it:

```bash
export ADMIN_KEY='vk_...'   # paste the key printed by valence init
```

`valence init` is idempotent: running it again on an initialized database does not issue a second key. Only a salted PBKDF2 digest is stored.

### 3. Start the service

```bash
export ORDER_STORE=sqlite
export SESSION_SERVICE=sqlite
export VOUCHER_DIR=./vouchers
export MODEL_PROVIDER=gemini          # or MODEL_PROVIDER=fake for an offline demo
export GOOGLE_API_KEY='...'           # omit when using the fake provider
export WEB_PASSCODE="$(python -c 'import secrets; print(secrets.token_hex(16))')"
export WEB_PASSCODE_SALT="$(python -c 'import secrets; print(secrets.token_hex(16))')"
export WEB_COOKIE_SECURE=0           # plain HTTP on localhost only
echo "Review passcode: $WEB_PASSCODE"

uv run uvicorn src.main:app --host 127.0.0.1 --port 8080
```

The passcode and salt gate the human-facing `/review` and `/loading` views; `WEB_COOKIE_SECURE=0` lets their login cookies work over plain HTTP on localhost. Keep it at `1` behind HTTPS. With `MODEL_PROVIDER=fake`, text intake works offline while photo and voice intake are capability-gated.

### 4. Apply the sample catalogue

In a second shell, with the service running and `ADMIN_KEY` set:

```bash
uv run valence setup \
  --url http://localhost:8080 \
  --admin-key "$ADMIN_KEY" \
  --fixture examples/acme-chemicals
```

`valence setup` applies the fixture over the admin API: the business profile and config, then customers, products, routes, delivery locations, and approvers. Omit any flag to be prompted. Config is read on every order, so a change takes effect without a restart.

### 5. Drive an order

```bash
curl localhost:8080/health

curl -X POST localhost:8080/api/v1/intake \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"sender_id": "+919812345001", "message": "2 drums sulfuric acid to Peenya"}'
```

Open `http://localhost:8080/review` with the passcode to see escalations, and `http://localhost:8080/loading` for the dispatch view. The interactive OpenAPI document is at `/openapi.json`.

---

## Quickstart: Docker Compose with Postgres

The Compose profile runs the app and Postgres, applies migrations on startup, and keeps orders and voucher XML in named volumes. It needs no cloud credentials.

```bash
cp .env.example .env
```

Edit `.env` and set at least:

- `POSTGRES_PASSWORD` — a strong, URL-safe value for the Postgres user.
- `GOOGLE_API_KEY` — a model credential, **or** `MODEL_PROVIDER=fake` for an offline demo.
- `WEB_PASSCODE` and `WEB_PASSCODE_SALT` — for the review and loading views.
- `WEB_COOKIE_SECURE=0` — only for plain HTTP on localhost; keep it at `1` behind HTTPS.
- `VALENCE_SETUP_TOKEN` — a strong secret used once for the HTTP bootstrap below.

```bash
docker compose up --build -d
curl localhost:8080/health   # {"status":"ok"}
```

The container entrypoint runs `alembic upgrade head` before starting uvicorn, then serves REST, the web views, and `/mcp` on port `8080`. Its healthcheck probes both `/health` and `/mcp`.

Bootstrap the first admin key over HTTP — Compose keeps Postgres on its internal network, while `POST /api/v1/setup` is available on the app port:

```bash
curl -X POST localhost:8080/api/v1/setup \
  -H 'Content-Type: application/json' \
  -d '{"setup_token":"<VALENCE_SETUP_TOKEN>","business_name":"Acme Chemicals","timezone":"Asia/Kolkata"}'
```

The response is the admin key; it is shown once. Clear `VALENCE_SETUP_TOKEN` from `.env` afterwards — the endpoint disables itself as soon as an admin key exists. Apply the catalogue from the host with `valence setup --url http://localhost:8080 --admin-key "$ADMIN_KEY" --fixture examples/acme-chemicals`.

This profile has no TLS reverse proxy or highly available database, so it is a portable deployment, not a production-ready one. `valence doctor` reports the state of the running configuration.

---

## Environment variables

The full, annotated list is in [`.env.example`](.env.example). These are the values an operator will actually set.

| Variable | Purpose | When required |
|----------|---------|---------------|
| `DATABASE_URL` | SQLAlchemy URL for orders, sessions, and catalogue | Always for migrations; defaults to `sqlite+aiosqlite:///./valence.db` |
| `POSTGRES_PASSWORD` | Password composed into `DATABASE_URL` by Compose | Compose profile |
| `ORDER_STORE`, `SESSION_SERVICE` | `sqlite`, `postgres`, `memory`, or `firestore` | Set both to `sqlite` locally, or leave both to Compose for Postgres |
| `MODEL_PROVIDER` | `gemini` (default), `fake`, or `fake-toolcall` | Always; use a fake provider offline |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Gemini credential for live intake | Live model runs |
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_CLOUD_PROJECT` | Vertex AI alternative to an API key | Vertex runs |
| `WEB_PASSCODE`, `WEB_PASSCODE_SALT` | Passcode and HMAC salt for the review/loading views | The web views; they return 503 without them |
| `WEB_COOKIE_SECURE` | Secure session cookies; set to `0` only for plain-HTTP localhost | Local HTTP runs |
| `VALENCE_SETUP_TOKEN` | One-time token for `POST /api/v1/setup` | HTTP bootstrap |
| `VALENCE_API_KEY` | Key given to the stdio MCP server and to client install snippets | stdio MCP; never authenticates an HTTP caller |
| `VALENCE_URL` | Client-side base URL; the MCP endpoint is `VALENCE_URL/mcp` | MCP client configuration |
| `VOUCHER_DIR`, `VOUCHER_BUCKET` | Filesystem directory or Cloud Storage bucket for voucher XML | Durable vouchers; empty means in-memory |
| `PORT` | Listening port, default `8080` | Non-default ports |
| `WEBHOOK_RATE_LIMIT_PER_SENDER` | Agent turns per sender per minute, default `30` | Tuning |
| `AGENT_TURN_TIMEOUT_S` | Deadline for one agent turn, default `120` | Tuning |
| `IDEMPOTENCY_TTL_S` | Replay window for stored intake/action results, default `86400` | Tuning; keep above `AGENT_TURN_TIMEOUT_S` |
| `AGENT_MAX_CONCURRENT_TURNS` | Cap on simultaneous turns, default `8` | Tuning |
| `ADK_FIRESTORE_ROOT_COLLECTION` | Firestore session root, default `adk-session` | Firestore adapter |
| `SERVICE_URL`, `TALLY_PUSH_URL` | CLI setup default URL; legacy fallback for the Tally push URL | Optional |

Secrets stay out of version control: `.env` is gitignored, API keys are stored only as salted digests and printed once, integration credentials are never returned by the API, and `valence doctor` redacts database URLs and reports credentials only as set or missing. Integration credentials for Meta, Gemini, and Tally are normally stored per Instance with `valence setup --integrations <file.json>` or `PUT /api/v1/integrations/{service}`.

---

## Migrations

Alembic configuration lives in `alembic.ini` and `migrations/versions/` (initial schema, auth bootstrap, and durable idempotency). Apply it locally with:

```bash
DATABASE_URL='sqlite+aiosqlite:///./valence.db' uv run alembic upgrade head
```

Compose and the Docker image run the same command on startup. `valence init` is a separate, idempotent bootstrap that creates any missing tables, seeds the catalogue, sets the business profile, and issues the first admin key; `valence doctor` checks that the database heads match the migration heads and tells you to run `alembic upgrade head` when they do not.

---

## Bootstrapping and configuration

- `valence init` — initialize an empty SQLite or Postgres database and issue the first admin key.
- `POST /api/v1/setup` — the equivalent HTTP path for deployments that cannot reach the database with the CLI. Guarded by `VALENCE_SETUP_TOKEN` in the body; disabled once an admin key exists.
- `valence setup` — apply `instance.json`, `catalog.json`, and `config.json` from a fixture such as `examples/acme-chemicals`, plus optional integration credentials.
- `POST /api/v1/keys` — issue smaller keys for the roles `admin`, `operator`, `approver`, and `viewer`. Approver keys are bound to an allowlisted phone.

The same operations are available as `GET`/`PUT /api/v1/catalog/{kind}`, `PATCH /api/v1/instance`, and `GET`/`PUT /api/v1/integrations/{service}`; health reports show configured/healthy status only, never secret values. All pre-v1 HTTP paths are retired; the old-to-new map is in [LEGACY_API.md](LEGACY_API.md).

---

## Agent Interface: REST and MCP

Every public route lives under `/api/v1`, plus `/health`, the Meta webhook at `/api/v1/webhooks/whatsapp`, and the bootstrap endpoint. All other routes require `Authorization: Bearer <key>`, where `<key>` is an issued Instance key obtained from `valence init`, `POST /api/v1/setup`, or `POST /api/v1/keys`. The `VALENCE_API_KEY` environment variable is only a client-side convenience for stdio MCP; it never authenticates an HTTP caller. Privileged order actions also require `confirm: true`. The OpenAPI document is served at `/openapi.json`.

### REST

| Method and path | Role | Purpose |
|-----------------|------|---------|
| `POST /api/v1/intake` | admin, operator | Place a text order from `sender_id` (E.164) and `message` |
| `POST /api/v1/intake/voice` | admin, operator | Ingest a recorded call as `audio_base64` |
| `POST /api/v1/intake/photo` | admin, operator | Ingest a photo of a handwritten order sheet |
| `GET /api/v1/orders` | any | List/filter orders, each with its `voucher_id` |
| `GET /api/v1/orders/{id}` | any | Order detail and event timeline |
| `PATCH /api/v1/orders/{id}` | approver | Edit fields of an order |
| `POST /api/v1/orders/{id}/actions` | approver | `approve`, `reject`, `dispatch`, `prepare_voucher`, `push_to_tally`, or `bill` |
| `GET /api/v1/orders/{id}/voucher` | approver | Download the prepared Tally voucher XML |
| `GET` / `POST /api/v1/loading-lists` | any / admin | Render one day or a window of Loading Lists |
| `GET /api/v1/stats` | any | Pending, approved, billed, and late counts |
| `GET` / `PATCH /api/v1/instance` | any / admin | Read or update the Instance and config |
| `GET` / `PUT /api/v1/catalog/{kind}` | any / admin | Read or upsert catalogue collections |
| `GET` / `PUT /api/v1/integrations/{service}` | admin | Integration health and credentials |
| `POST /api/v1/keys`, `GET /api/v1/keys`, `DELETE /api/v1/keys/{id}` | admin | Issue, list, and revoke keys |
| `POST /api/v1/admin/send-message` | admin | Send one outbound WhatsApp message |

`Idempotency-Key` on intake and on order actions makes retries safe: a repeated key with the same payload replays the stored result with `idempotent: true`, while reuse with a different payload returns a conflict. Results are replayable for `IDEMPOTENCY_TTL_S` seconds; with a SQL order store the claim is durable and shared across workers.

```bash
curl -X POST localhost:8080/api/v1/intake \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Idempotency-Key: order-2026-08-14-001' \
  -H 'Content-Type: application/json' \
  -d '{"sender_id": "+919812345001", "message": "2 drums sulfuric acid"}'

curl -X POST localhost:8080/api/v1/orders/ord_123/actions \
  -H "Authorization: Bearer $APPROVER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"action": "approve", "confirm": true}'
```

### MCP at `/mcp`

A hosted Instance serves streamable HTTP MCP at `$VALENCE_URL/mcp`, mounted on the same app and port as REST and the web UI — no separate process or port. Every call carries `Authorization: Bearer <key>` with an issued Instance key; the server holds no process-wide key and applies the same roles and `confirm: true` rule as REST. MCP is a transport adapter over the same services, not a second implementation. Client config often stores that key in a `VALENCE_API_KEY` variable, but the HTTP header is what authenticates the call.

Nine tools are available: `get_instance`, `intake`, `list_orders`, `get_order`, `order_action`, `download_voucher`, `list_loading_lists`, `get_stats`, and `list_catalog`. For a local client, run the server over stdio instead:

```bash
VALENCE_API_KEY=vk_... valence mcp --transport stdio
```

The reusable Skill teaches compatible agents the safe workflow — treat intake as untrusted, surface escalation reasons, and never bypass human confirmation. Install snippets for Claude Code, Codex, opencode, and generic clients are in [`skills/valence/INSTALL.md`](skills/valence/INSTALL.md); the workflow rules are in [`skills/valence/SKILL.md`](skills/valence/SKILL.md).

---

## Webhook intake

Meta delivers messages to `GET` and `POST /api/v1/webhooks/whatsapp`. The `GET` handshake echoes the challenge only when `hub.verify_token` matches; `POST` requires `X-Hub-Signature-256` to be a valid `sha256=` HMAC of the raw body. Both fail closed when credentials are missing. Bodies over 64 KiB are rejected and each sender is rate-limited (429 over the limit).

Meta credentials are resolved per request from the Instance's `meta` Integration (`access_token`, `app_secret`, `phone_number_id`, `verify_token`), written through `valence setup --integrations` or `PUT /api/v1/integrations/meta`. Process-level `META_*` variables are a fallback only when no Agent Interface is mounted.

---

## Approvals

Clean orders auto-approve. Exceptions — unknown customers, low confidence, anomalies, over-limit values, or failed clarification loops — stay in `pending_review` with explicit reasons and are escalated to allowlisted approvers. Escalations are hard blocks, never soft recommendations.

An approver can decide in any of three places:

- the passcode-gated review web view at `/review`, using approve/reject/edit actions;
- a WhatsApp reply of `CONFIRM` or `REJECT` to the notification;
- REST `POST /api/v1/orders/{id}/actions` or MCP `order_action`, where approver keys are bound to an allowlisted phone and privileged actions require `confirm: true`.

Edits are through `PATCH /api/v1/orders/{id}` or the review view — never from a customer message. Every decision lands on the order's append-only event timeline.

---

## Vouchers, billing, and Tally

`prepare_voucher` locks line rates, derives CGST+SGST or IGST from the delivery and customer states, and uses only mapped Tally masters; an unmapped master blocks generation with an explicit error. The resulting XML is stored per voucher and stamped on the order.

- Prepare with `POST /api/v1/orders/{id}/actions` (`prepare_voucher`, `confirm: true`), the review view, or MCP `order_action`.
- Download with `GET /api/v1/orders/{id}/voucher`, the review view, or MCP `download_voucher`.
- Push with the `push_to_tally` action or the review view's push button. The URL comes from the Instance's `tally` Integration; `TALLY_PUSH_URL` is only a legacy fallback. `scripts/tally_sync.py` polls orders and pushes XML for an unattended Tally inbox.
- Mark an order billed with the `bill` action so billing counts and the audit trail stay accurate.

Voucher storage is `VOUCHER_BUCKET` (Cloud Storage), else `VOUCHER_DIR` (filesystem), else in-memory.

---

## Loading Lists and cutoff

A Loading List is a live, price-free render of approved orders for a delivery day, grouped by route with an `unrouted` bucket. It is not a stored snapshot. Orders approved after the configured daily cutoff are "late" and appear in a separate add-on section, with a WhatsApp heads-up to dispatch.

- Human view: `GET /loading?day=YYYY-MM-DD`, passcode-gated, with a print button and per-row dispatch actions.
- Agent Interface: `GET /api/v1/loading-lists?day=...`, `POST /api/v1/loading-lists` with `{"day": ...}` or a `window`, and MCP `list_loading_lists` / `get_stats`. The REST trigger also raises late-order heads-ups; a window spans at most 30 days.
- CLI: `valence cutoff` renders the same lists as JSON without sending heads-ups.

The cutoff time and business timezone come from the Instance config (defaults `17:30` and `Asia/Kolkata`).

---

## Operations CLI

| Command | What it does |
|---------|--------------|
| `valence init --business-name "Acme Chemicals"` | Create the schema, seed the catalogue, and issue the first admin key |
| `valence setup --url ... --admin-key ... --fixture ...` | Apply catalogue, config, and integrations over the admin API |
| `valence doctor` | Read-only pre-flight checks; exits non-zero if any check fails |
| `valence cutoff --day 2026-08-14` | Render a Loading List as JSON on stdout |
| `valence mcp --transport stdio` | Run the MCP server over stdio for a local client |

```bash
valence doctor --database-url sqlite+aiosqlite:///./valence.db
valence doctor --json

valence cutoff --day 2026-08-14
valence cutoff --date 2026-08-14
valence cutoff --start 2026-08-14 --end 2026-08-16 --output lists.json
```

`valence doctor` checks Python, configuration, database connectivity, migration heads, model provider, voucher store, Tally, WhatsApp, and the MCP server. It never contacts a provider and never prints a secret value. `valence cutoff` accepts `--day`/`--date` or `--start` with `--end`, and `--output` writes JSON to a file instead of stdout. Both accept `--database-url` (default `DATABASE_URL`) and work with SQLite and Postgres. Run `valence doctor --help` or `valence cutoff --help` for all options.

---

## Verification and development

The real-process end-to-end harness boots an offline Instance from scratch: it runs `valence init` and `valence setup` as CLI subprocesses, starts the actual uvicorn app, drives it over HTTP, pushes a voucher to a local fake Tally, delivers a signed Meta webhook, restarts the process, and verifies that orders, vouchers, and Loading Lists survived. Every step prints `PASS`/`FAIL`, and artifacts are kept for diagnosis.

```bash
uv run python scripts/e2e_harness.py
uv run python scripts/e2e_harness.py --artifacts ./e2e-artifacts
uv run python scripts/e2e_harness.py --database-url postgresql+asyncpg://user:pass@localhost:5432/valence
uv run pytest tests/test_e2e_harness.py
```

This is the same flow as the quickstart above, with `MODEL_PROVIDER=fake-toolcall` and no external credentials. The default uses a clean SQLite Instance in a fresh temp directory; `--database-url` runs the same flow on Postgres.

Developer commands:

```bash
uv sync --all-extras
uv run pytest
uv run pytest tests/test_core.py
uv run ruff check .
uv run mypy src
uv lock --check
docker compose up --build
```

To exercise the agent against a live model, use `python scripts/eval_agent.py` (`--list` to enumerate cases, `--category safety` to filter), with the model variables from the table above set.

### Continuous Integration

The workflow in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull request and on pushes to `main`. It installs from the committed `uv.lock` on Python 3.12 and runs the same lint, typecheck, and test commands as local development:

- **Lint, types and tests (SQLite)** — `ruff check`, `mypy src`, the full `pytest` suite, and the end-to-end harness against a clean SQLite Instance.
- **Postgres conformance and end-to-end** — a Postgres service container, the harness with `--database-url` against a disposable database, then `pytest -m postgres` with `TEST_POSTGRES_URL`.
- **Dependency audit** — `uv export --frozen` piped to a pinned `pip-audit-extra --fail-level HIGH`, gating on high and critical advisories.

CI needs no credentials: the harness runs with fake providers, empty Meta credentials, and a local fake Tally. E2E artifacts are uploaded on failure. Recommended branch protection for `main`: require the three CI checks, require an approving review, dismiss stale approvals on new commits, require branches to be up to date, and block force pushes and branch deletion. This keeps `main` deployable while the final integration PR remains a deliberate human decision.

---

## Project layout

```
valence/
├── src/                   # FastAPI app, agent, tools, stores, CLI, MCP gateway
├── tests/                 # pytest suite, including the end-to-end harness
├── scripts/               # docker-entrypoint.sh, e2e_harness.py, eval_agent.py, helpers
├── migrations/versions/   # Alembic revisions
├── examples/acme-chemicals/  # fixture for valence setup
├── skills/valence/        # reusable Skill and client install snippets
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── alembic.ini
├── LEGACY_API.md
└── pyproject.toml
```

---

## License

MIT
