#!/usr/bin/env bash
#
# LEGACY: GCP provisioning for the retired deployment. The project is expired;
# this script is kept for historical reference until archival. The former
# Pub/Sub cutoff chain targeted the retired /api/cutoff route and has been
# removed; a v1 Instance renders cutoff through `valence cutoff` or
# POST /api/v1/loading-lists with an admin API key (see LEGACY_API.md).
#
# Single infrastructure-provisioning point for the Order Intake & Fulfillment
# agent. From a fresh GCP project this creates and wires:
#
#   - Cloud Run service      (initial deploy from local source)
#   - Firestore database     (native mode, seeded via src.seed_firestore)
#   - Cloud Storage bucket   (holds Tally voucher XML, used from ticket 7)
#   - IAM bindings           (Cloud Run -> Firestore/Storage)
#   - Secrets                (Gemini API key + channel/deploy secrets, never committed)
#   - Cloud Build trigger    (auto-deploy on push to main)
#
# Prereqs: gcloud CLI authenticated with a project owner/editor, and the
# Secret Manager, Artifact Registry, Cloud Build and Run APIs available to your
# billing account. Set PROJECT_ID (required) and, if you want the GitHub
# auto-deploy trigger, _GITHUB_REPO (e.g. "not3zra/valence").
#
# Usage:  PROJECT_ID=my-project ./infra/provision.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (your GCP project id)}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-valence-images}"
SERVICE="${SERVICE:-valence}"
RUN_SA="${RUN_SA:-valence-cloudrun}"
GITHUB_REPO="${GITHUB_REPO:-}"

RUNTIME_SA_EMAIL="${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
error() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null || error "gcloud CLI not found — see https://cloud.google.com/sdk/docs/install"

gcloud config set project "$PROJECT_ID"

log "Enabling required APIs"
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com \
  --project="$PROJECT_ID"

log "Creating Artifact Registry repo: $REPO"
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --project="$PROJECT_ID" || true

log "Creating Firestore database (native mode, default)"
if ! gcloud firestore databases describe --database="(default)" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --database="(default)" \
    --location="$REGION" \
    --project="$PROJECT_ID"
else
  echo "Firestore database already exists."
fi

BUCKET="valence-${PROJECT_ID}-vouchers"
log "Creating Cloud Storage bucket: gs://${BUCKET}"
gcloud storage buckets create "gs://${BUCKET}" \
  --location="$REGION" \
  --project="$PROJECT_ID" || true

log "Creating service accounts"
gcloud iam service-accounts create "$RUN_SA" \
  --display-name="Valence Cloud Run runtime" \
  --project="$PROJECT_ID" || true

log "Granting Cloud Run -> Firestore + Cloud Storage roles to $RUN_SA"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role=roles/datastore.user --condition=None >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role=roles/storage.objectUser --condition=None >/dev/null

log "Provisioning secrets (values come from the environment / prompts, never the repo)"
read_secret() { # name envvar
  local name="$1" envvar="$2" value
  if [[ -n "${!envvar:-}" ]]; then
    value="${!envvar}"
  else
    printf 'Value for secret %s: ' "$name" >&2
    read -rs value >&2
    printf '\n' >&2
  fi
  printf '%s' "$value"
}
for spec in \
  "GEMINI_API_KEY:GEMINI_API_KEY" \
  "META_APP_SECRET:META_APP_SECRET" \
  "META_VERIFY_TOKEN:META_VERIFY_TOKEN" \
  "META_ACCESS_TOKEN:META_ACCESS_TOKEN" \
  "META_PHONE_NUMBER_ID:META_PHONE_NUMBER_ID" \
  "WEB_PASSCODE:WEB_PASSCODE" \
  "WEB_PASSCODE_SALT:WEB_PASSCODE_SALT"; do
  name="${spec%%:*}"
  envvar="${spec##*:}"
  if gcloud secrets describe "$name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "Secret $name already exists."
  else
    value="$(read_secret "$name" "$envvar")"
    [[ -n "$value" ]] || error "no value supplied for secret $name"
    printf '%s' "$value" | gcloud secrets create "$name" \
      --data-file=- --project="$PROJECT_ID" >/dev/null
    unset value
  fi
done

log "Granting Secret Manager accessor to $RUN_SA"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role=roles/secretmanager.secretAccessor --condition=None >/dev/null

log "Initial deploy from local source (creates the Cloud Run service)"
# Gemini is served through Vertex AI (ADC via the runtime SA), not the
# free-tier API key. asia-southeast1 is the only region serving
# gemini-3.5-flash on this project (all US/EU regions 404).
gcloud run deploy "$SERVICE" \
  --source . \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --service-account="$RUNTIME_SA_EMAIL" \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=asia-southeast1,VOUCHER_BUCKET=valence-${PROJECT_ID}-vouchers" \
  --set-secrets=META_APP_SECRET=META_APP_SECRET:latest \
  --set-secrets=META_VERIFY_TOKEN=META_VERIFY_TOKEN:latest \
  --set-secrets=META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest \
  --set-secrets=META_PHONE_NUMBER_ID=META_PHONE_NUMBER_ID:latest \
  --set-secrets=WEB_PASSCODE=WEB_PASSCODE:latest \
  --set-secrets=WEB_PASSCODE_SALT=WEB_PASSCODE_SALT:latest \
  --project="$PROJECT_ID"

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
  --region="$REGION" --platform=managed --project="$PROJECT_ID" \
  --format='value(status.url)')"
log "Service live at $SERVICE_URL"

# The former Cloud Scheduler -> Pub/Sub -> /api/cutoff chain is gone with the
# retired endpoint. A v1 Instance triggers a cutoff render with an admin API
# key through `valence cutoff` or POST /api/v1/loading-lists (LEGACY_API.md).

if [[ -n "$GITHUB_REPO" ]]; then
  log "Creating Cloud Build trigger: auto-deploy on push to main of $GITHUB_REPO"
  if ! gcloud builds triggers create github \
    --repo-owner="${GITHUB_REPO%/*}" \
    --repo-name="${GITHUB_REPO#*/}" \
    --branch-pattern='^main$' \
    --build-config=cloudbuild.yaml \
    --substitutions="_REGION=${REGION},_REPO=${REPO},_SERVICE=${SERVICE},_RUN_SA=${RUN_SA}" \
    --name="valence-deploy-main" \
    --project="$PROJECT_ID"; then
    error "Trigger creation failed. Connect $GITHUB_REPO in the Cloud Build console, then re-run: gcloud builds triggers create github --repo-owner=${GITHUB_REPO%/*} --repo-name=${GITHUB_REPO#*/} --branch-pattern='^main$' --build-config=cloudbuild.yaml --name=valence-deploy-main."
  fi
else
  echo "Skipped GitHub auto-deploy trigger (set GITHUB_REPO=owner/name to create it)."
fi

log "Seeding Firestore"
python3 -m src.seed_firestore

log "Done. Verify:"
echo "  1. Open the Cloud Run dashboard and open \$SERVICE_URL (health page)."
echo "  2. POST /api/v1/intake to exercise a v1 Instance (message in -> reply out)."
echo "  3. Feed a recorded call batch with scripts/feed_voice.py (VALENCE_API_KEY)."
echo "  4. Push to main now auto-deploys via the Cloud Build trigger."
