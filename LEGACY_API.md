# Retired legacy API paths (issue #99)

The five pre-v1 HTTP paths below are **retired**. They are no longer
registered on the app: each returns `404 Not Found`, and none appears in the
OpenAPI document at `/openapi.json`. The Agent Interface under `/api/v1` (and
the streamable HTTP MCP endpoint at `/mcp`) is the only public API; the
human-facing `/review` and `/loading` web views are unchanged.

All v1 routes except `/health`, the Meta webhook, and the bootstrap
`POST /api/v1/setup` (which carries its `setup_token` in the body) require
`Authorization: Bearer <VALENCE_API_KEY>`. Obtain a key with `valence init`
(or `POST /api/v1/setup`), then issue narrow keys with `POST /api/v1/keys`.
Roles are `admin`, `operator`, `approver`, and `viewer`; order actions also
require `confirm: true`.

## Old-to-new mapping

| Retired path | Method | Replacement | Notes |
|---|---|---|---|
| `/api/roundtrip` | POST | `POST /api/v1/intake` | The token-gated debug probe is gone. Send `{"sender_id": "<E.164>", "message": "..."}`. An intake-role API key is required: the key's principal authorizes the call, while `sender_id` is the customer/session identity, not an authorization identity. `Idempotency-Key` is accepted. |
| `/api/voice/ingest` | POST | `POST /api/v1/intake/voice` | Same payload shape (`caller`, `audio_base64`, `mime_type`). The per-deploy bearer token is replaced by an intake-role API key; audio support remains capability-gated (503 when the provider cannot read audio). |
| `/api/whatsapp/webhook` | GET, POST | `GET` / `POST /api/v1/webhooks/whatsapp` | Same Meta signature/verify-token contract. The handler was already mounted at the v1 path; only the legacy aliases were removed. Per-Instance Meta Integration credentials still apply. |
| `/api/vouchers` | GET | `GET /api/v1/orders` | There is no v1 aggregate voucher route: `GET /api/v1/orders` returns each order's `voucher_id`. `scripts/tally_sync.py` projects the old polling shape from that listing. Per-order XML is at `GET /api/v1/orders/{order_id}/voucher`. |
| `/api/cutoff` | POST | `POST /api/v1/loading-lists` | The Pub/Sub-envelope + `CUTOFF_SECRET` gate is gone. An admin key posts `{"day": "YYYY-MM-DD"}` (or a `window`); `POST /api/v1/cutoff/render` remains as a deprecated compatible alias. `valence cutoff` is the CLI equivalent for rendering the lists locally; unlike the REST trigger it does not raise late-order WhatsApp heads-ups. |

## Configuration removed with the routes

- `ROUNDTRIP_TOKEN` — gated the roundtrip probe.
- `VOICE_INGEST_TOKEN` — gated the company-recorded call ingest endpoint.
- `CUTOFF_SECRET` — gated the scheduler's Pub/Sub cutoff push.

These environment values are ignored by the current app. Use `VALENCE_API_KEY`
(and the roles above) instead of all three.

## Scripts migrated

- `scripts/feed_voice.py` now posts to `/api/v1/intake/voice` using
  `--api-key` / `VALENCE_API_KEY`.
- `scripts/smoke_whatsapp_webhook.py` drives `/api/v1/webhooks/whatsapp`.
- `scripts/tally_sync.py` lists through `/api/v1/orders` and downloads through
  `/api/v1/orders/{order_id}/voucher` with an approver-role `VALENCE_API_KEY`.

The legacy GCP provisioning and Cloud Build files have been removed from the
release. `scripts/run_local.sh` remains a local development helper; the
`valence` CLI and Docker Compose path are the supported way to run a v1
Instance.

## Verification

`tests/test_legacy_api_retirement.py` asserts that every retired path returns
404, that none appears in OpenAPI, and that the v1 replacements resolve. The
existing v1 contract tests cover the behaviour in detail.
