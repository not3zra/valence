#!/usr/bin/env bash
#
# Run the service locally against the Firestore emulator.
#
#  1. starts the Cloud Firestore emulator on port 8686
#  2. seeds Firestore with the canonical data (src.seed_firestore)
#  3. runs uvicorn on PORT (default 8080); Ctrl+C stops the emulator too
#
# Prereqs: gcloud CLI with the cloud-firestore-emulator component:
#   gcloud components install cloud-firestore-emulator
set -euo pipefail

PORT="${PORT:-8080}"
EMULATOR_PORT="${EMULATOR_PORT:-8686}"
PROJECT_ID="${PROJECT_ID:-valence-local}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud CLI not found — install it and the cloud-firestore-emulator component." >&2
  exit 1
fi

export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export FIRESTORE_EMULATOR_HOST="localhost:${EMULATOR_PORT}"
export FIRESTORE_PROJECT_ID="$PROJECT_ID"
# This path runs against the Firestore emulator, so select the optional GCP
# adapters explicitly (the default install runs in memory).
export SESSION_SERVICE=firestore

generate_secret() {
  python3 -c 'import secrets; print(secrets.token_hex(16))'
}

# Local-only auth values: use exported values when present, otherwise generate
# fresh random values for this run. Never ship a demo passcode or token.
export ROUNDTRIP_TOKEN="${ROUNDTRIP_TOKEN:-$(generate_secret)}"
export VOICE_INGEST_TOKEN="${VOICE_INGEST_TOKEN:-$(generate_secret)}"
export WEB_PASSCODE="${WEB_PASSCODE:-$(generate_secret)}"
export WEB_PASSCODE_SALT="${WEB_PASSCODE_SALT:-$(generate_secret)}"
export WEB_COOKIE_SECURE="${WEB_COOKIE_SECURE:-0}"
# Local dev uses the in-memory voucher store (no bucket provisioned).
export VOUCHER_BUCKET="${VOUCHER_BUCKET:-}"

echo "==> Local dev credentials:"
echo "    ROUNDTRIP_TOKEN=$ROUNDTRIP_TOKEN"
echo "    VOICE_INGEST_TOKEN=$VOICE_INGEST_TOKEN"
echo "    WEB_PASSCODE=$WEB_PASSCODE"

# Model access: set GOOGLE_API_KEY, or opt into Vertex AI with
# GOOGLE_GENAI_USE_VERTEXAI=true and a project/location. For Vertex, set
# PROJECT_ID to the target GCP project (it also namespaces the emulator).
if [[ "${GOOGLE_GENAI_USE_VERTEXAI:-}" == "true" ]]; then
  : "${GOOGLE_CLOUD_LOCATION:?Set GOOGLE_CLOUD_LOCATION when using Vertex AI}"
  if [[ "$PROJECT_ID" == "valence-local" ]]; then
    echo "Set PROJECT_ID to your GCP project when using Vertex AI." >&2
    exit 1
  fi
  echo "==> Model: Vertex AI in $GOOGLE_CLOUD_PROJECT ($GOOGLE_CLOUD_LOCATION)"
else
  : "${GOOGLE_API_KEY:?Set GOOGLE_API_KEY, or set GOOGLE_GENAI_USE_VERTEXAI=true for Vertex AI}"
  echo "==> Model: Gemini via GOOGLE_API_KEY"
fi

echo "==> Starting Firestore emulator on :$EMULATOR_PORT"
gcloud beta emulators firestore start \
  --host-port="localhost:${EMULATOR_PORT}" \
  --project="$PROJECT_ID" >/tmp/firestore-emulator.log 2>&1 &
EMULATOR_PID=$!

cleanup() {
  echo "==> Stopping Firestore emulator (pid $EMULATOR_PID)"
  kill "$EMULATOR_PID" 2>/dev/null || true
  wait "$EMULATOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

up=false
for _ in $(seq 1 30); do
  if curl -sf "http://localhost:${EMULATOR_PORT}/" >/dev/null 2>&1; then
    up=true
    break
  fi
  sleep 1
done
if [[ "$up" != "true" ]]; then
  echo "Firestore emulator did not come up on :$EMULATOR_PORT — see /tmp/firestore-emulator.log" >&2
  exit 1
fi

echo "==> Seeding Firestore"
python -m src.seed_firestore

echo "==> Starting Valence on :$PORT (Ctrl+C to stop)"
echo "    (Firestore emulator log: /tmp/firestore-emulator.log)"
uvicorn src.main:app --host 0.0.0.0 --port "$PORT"