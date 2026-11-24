# Changelog

## v1.0.0 - 2026-11-25

Valence v1 is the portable, independently deployable release: one Instance
runs without GCP, using Postgres in production or SQLite locally.

### Changes

- Portable persistence with SQLAlchemy async adapters: Postgres in production,
  SQLite for local development and tests, and optional GCP adapters behind the
  `[gcp]` extra.
- Stable `/api/v1` Agent Interface covering intake, orders, order actions,
  loading lists, stats, catalogue, setup, keys, integrations, and the WhatsApp
  webhook.
- Streamable HTTP MCP mounted at `/mcp`, plus the existing stdio transport.
- Operations CLI: `valence init`, `valence setup`, `valence doctor`,
  `valence cutoff`, and `valence mcp`.
- Durable, concurrency-safe idempotency for intake and confirmed order actions,
  with expiry cleanup for the SQL-backed store.
- Real-process end-to-end harness covering MCP, signed WhatsApp webhooks, fake
  Tally push, loading lists, billing, and restart persistence.
- GitHub Actions CI for lint, types, SQLite tests, Postgres conformance, E2E,
  and a high/critical dependency audit.
- Legacy non-v1 API routes retired; `LEGACY_API.md` maps old paths to their v1
  replacements.
- README rewritten for the portable v1 install, setup, and operations flow.
- Legacy GCP deployment artifacts removed (`cloudbuild.yaml`,
  `infra/provision.sh`); GCP is optional, not required.
- Security release-gate fixes: vulnerable transitive dependencies bumped and
  expired idempotency rows now reclaimed periodically.

### Supported surface

- **Runtime:** Python 3.12; Postgres (CI uses 16) with `asyncpg`, or SQLite with
  `aiosqlite`; Docker Compose profile.
- **Channels:** WhatsApp Cloud API text and photo intake; REST voice and photo
  intake; web review queue.
- **Agent Interface:** REST `/api/v1` with OpenAPI, and MCP over stdio or
  streamable HTTP at `/mcp`.
- **Models:** native Gemini provider and the deterministic `fake-toolcall`
  provider for tests and local E2E. Voice and photo are capability-gated.
- **Auth:** per-Instance API keys with `admin`, `operator`, `approver`, and
  `viewer` roles; privileged actions require `confirm: true`.
- **Vouchers and Tally:** filesystem vouchers by default, in-memory for tests,
  optional GCS with the `[gcp]` extra, and external Tally push.
- **Operations:** catalogue and integration setup, loading lists, cutoff,
  billing status, stats, and `valence doctor` health checks.

### Security

- Final STRIDE audit has no open hard findings. Residual judgment calls and
  hardening work are ticketed post-v1.
- `pip-audit` runs at HIGH severity in CI; the v1 lockfile has no known
  high/critical advisories.
- No live secrets or legacy GCP deployment artifacts are tracked in the release
  commit.

### Known limitations

These are deliberate v1 boundaries with post-v1 tickets:

- ADK imports are not fully confined to the agent boundary (#112).
- Native Anthropic, OpenAI, and LiteLLM adapters are not included
  (#107, #108, #109, #110).
- S3-compatible voucher storage is not included (#111).
- Per-key rate limits and additional body caps are not included (#113).
- Credential encryption and CSRF protection are not included (#114).
- Setup-lock and MCP input-hardening work remains (#115).
- Backup and restore verification remains (#116).
- GCP secret rotation and local cleanup remain (#117).
- Normalized schema expansion, migration, and contract work remains
  (#104, #105, #106).

### Verification

- SQLite: 656 passed, 38 skipped.
- Postgres (full suite with `TEST_POSTGRES_URL`): 670 passed, 24 skipped.
- E2E: PASS on SQLite and Postgres, including MCP, signed webhook, fake Tally,
  and restart persistence.
- `ruff` clean; `mypy` clean across 42 source files; dependency audit clean at
  HIGH severity.
