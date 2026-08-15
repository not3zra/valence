# Threat Model

The system's declared trust boundaries, the components and data flows on either
side of them, and the running record of STRIDE security audits. The
`audited-through` marker is the fixed point for the next audit; prior runs are
kept for history.

## Trust boundaries

- **Internet -> Cloud Run** — the whole service is deployed `--allow-unauthenticated`.
  Entry points behind it: `/health` (public, no data), `/api/roundtrip` (Bearer
  probe, gated by `ROUNDTRIP_TOKEN`, closed 503 when unset), `/api/whatsapp/webhook`
  and `/api/voice/callback` (Twilio-HMAC-signed), and the passcode-gated
  `/review*` web view.
- **Twilio -> webhooks** — identity is the Twilio Auth Token: every webhook
  request is verified with HMAC-SHA1 `X-Twilio-Signature`; a missing signature
  or unset token is rejected.
- **Web approver -> review view** — a per-deploy passcode (`WEB_PASSCODE`,
  never a public seed) with a per-deploy salt (`WEB_PASSCODE_SALT`) minting an
  httponly `valence_review` cookie holding an HMAC-SHA256 digest keyed by the
  salt; the digest is unforgeable without the salt even if the passcode leaks.
- **Agent -> Order Processing Core -> Firestore** — the ADK agent's `process_order`
  and `approve_order` tools talk only to the core; the core talks only to the
  store seam. Approver identity in `approve_order` is the ADK session `user_id`
  (the Twilio-verified sender phone), re-checked against the allowlist in the core.
- **Media fetch** — outbound fetches of Twilio media/recordings are SSRF-guarded:
  allowlisted hosts, https only, no redirects, 5 MiB cap, basic auth.

## Audit record

### 2026-08-15 — remediation of findings #27 / #28 (fixes stacked on the same feature branch)

Scope: the two hard findings from the first audit, fixed before the #23 stack
merged. Tooling: pytest 220 passed, ruff clean, mypy clean.

Remediated:

- **#27 — Forgeable review web-view gate**: passcode is now a per-deploy secret
  (`WEB_PASSCODE`, no public seed); the session cookie is an HMAC-SHA256 digest
  keyed by `WEB_PASSCODE_SALT`, unforgeable without the salt; cookie hardened
  with `secure` + `max_age`; state-changing `/review` POSTs reject cross-origin
  requests (Origin hostname vs request Host header — the Host is set by the LB
  to the public host, so Cloud Run's proxy scheme skew can't false-reject).
  Remaining judgment calls: the local demo passcode is still `valence-demo` behind
  `run_local.sh` (clearly local-only), cookie rotation/expiry-on-restart not
  implemented, web decisions still log actor `"web"` with no per-user identity.
- **#28 — Unauthenticated `/api/roundtrip` probe**: the probe now requires a
  dedicated `ROUNDTRIP_TOKEN` Bearer token and returns 503 when none is
  configured — never open. Decoupled from `TWILIO_AUTH_TOKEN`.

Carried over (judgment calls, not routed):

- **DoS / cost amplification**: `list_all_orders` streams the full orders
  collection per queue render and per 10 s stats poll; `clear_pending_approvals_for_order`
  scans the full `pending_approvals` collection per decision; search runs an
  N+1 `list_order_events` query per order; no rate-limiting on login/decision.
- **Repudiation**: bare `except Exception` in the web `_decide` handler swallows
  failed approval attempts without an audit record.
- **Stale pending approvals**: the web decision path never clears
  `pending_approvals`, so decided orders accumulate stale WhatsApp-pending
  entries (harmless — the status guard blocks acting on them, but they grow).

### 2026-08-15 — `issue-6-review-editing` branch feature audit (521b1c1..e73e7ed)

Scope: WhatsApp confirm approval path (#7), review web view (#6/#24), edit
orders (#6 follow-up), plus two fixes. First audit; no prior findings, so all
findings below are New. Tooling: pytest 218 passed, ruff clean, mypy clean,
pip-audit no known vulnerabilities; no CI SAST/dependency gate in CI
(cloudbuild only builds + deploys).

Hard findings (routed to the tracker, `security` + `needs-human-review`):

- **Forgeable review web-view gate** (Spoofing / Elevation / Info Disclosure /
  Repudiation): the seed `web_passcode` is the public `valence-demo`, and the
  session cookie is an unsalted `sha256(passcode)` digest, so anyone can mint a
  valid cookie and act as an approver / read all order data; web decisions log
  actor `"web"` and edits log no actor. → [#27](https://github.com/not3zra/valence/issues/27)
- **`/api/roundtrip` probe impersonates an approver** (Spoofing / Elevation,
  composition): with `TWILIO_AUTH_TOKEN` unset the probe is unauthenticated and
  accepts a caller-supplied `sender_id`; combined with the new `approve_order`
  tool an attacker can approve/reject any escalated order as an allowlisted
  approver. → [#28](https://github.com/not3zra/valence/issues/28)

Judgment calls (carried in report only, not routed):

- **CSRF** on the decision/edit POST endpoints: protected only by
  `samesite=lax`, no Origin/Referer check; not a proven path in modern browsers.
- **Cookie hardening**: no `secure` flag; no expiry/rotation on the session cookie.
- **DoS / cost amplification**: `list_all_orders` streams the full orders
  collection per queue render and per 10 s stats poll; `clear_pending_approvals_for_order`
  scans the full `pending_approvals` collection per decision; search runs an
  N+1 `list_order_events` query per order; no rate-limiting on login/decision.
- **Repudiation**: bare `except Exception` in the web `_decide` handler swallows
  failed approval attempts without an audit record.
- **Stale pending approvals**: the web decision path never clears
  `pending_approvals`, so decided orders accumulate stale WhatsApp-pending
  entries (harmless — the status guard blocks acting on them, but they grow).

Lines fixed since audit start: url-encoding of `order_id` in edit redirects
(`e73e7ed`, from a prior per-diff security review) confirmed present.

audited-through: 2f7bdc1