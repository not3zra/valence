#!/usr/bin/env bash
#
# Single infrastructure-provisioning point for the Order Intake & Fulfillment
# agent. From a fresh GCP project this creates and wires:
#
#   - Cloud Run service      (initial deploy from local source)
#   - Firestore database     (native mode, seeded via src.seed_firestore)
#   - Cloud Storage bucket   (holds Tally voucher XML, used from ticket 7)
#   - Pub/Sub topic + push   (feeds the Cutoff chain, used from ticket 8)
#   - Cloud Scheduler job    (daily Cutoff trigger -> topic -> Cloud Run)
#   - IAM bindings           (Cloud Run -> Firestore/Storage; push sub -> Cloud Run)
#   - Secrets                (Gemini API key + Twilio credentials, never committed)
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
PUSH_SA="${PUSH_SA:-valence-cutoff-push}"
SCHED_SA="${SCHED_SA:-valence-cutoff-sched}"
TOPIC="${TOPIC:-valence-cutoff}"
SCHEDULE="${SCHEDULE:-31 17 * * *}"        # daily 17:31, just after the 17:30 cutoff
SCHEDULE_TZ="${SCHEDULE_TZ:-Asia/Kolkata}"
GITHUB_REPO="${GITHUB_REPO:-}"

RUNTIME_SA_EMAIL="${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
PUSH_SA_EMAIL="${PUSH_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA_EMAIL="${SCHED_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

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

log "Creating Pub/Sub topic + push subscription (Cutoff chain, used from ticket 8)"
gcloud pubsub topics create "$TOPIC" --project="$PROJECT_ID" || true
# The subscription's push endpoint is attached after the first deploy so the
# Cloud Run URL is known; see "attaching the push endpoint" below.

log "Creating service accounts"
gcloud iam service-accounts create "$RUN_SA" \
  --display-name="Valence Cloud Run runtime" \
  --project="$PROJECT_ID" || true
gcloud iam service-accounts create "$PUSH_SA" \
  --display-name="Valence Cutoff push subscription" \
  --project="$PROJECT_ID" || true
gcloud iam service-accounts create "$SCHED_SA" \
  --display-name="Valence Cutoff Cloud Scheduler" \
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
  "TWILIO_ACCOUNT_SID:TWILIO_ACCOUNT_SID" \
  "TWILIO_AUTH_TOKEN:TWILIO_AUTH_TOKEN" \
  "TWILIO_WHATSAPP_FROM:TWILIO_WHATSAPP_FROM" \
  "TWILIO_WHATSAPP_JOIN_CODE:TWILIO_WHATSAPP_JOIN_CODE" \
  "ROUNDTRIP_TOKEN:ROUNDTRIP_TOKEN" \
  "WEB_PASSCODE:WEB_PASSCODE" \
  "WEB_PASSCODE_SALT:WEB_PASSCODE_SALT" \
  "CUTOFF_SECRET:CUTOFF_SECRET"; do
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
gcloud run deploy "$SERVICE" \
  --source . \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --service-account="$RUNTIME_SA_EMAIL" \
  --set-secrets=GOOGLE_API_KEY=GEMINI_API_KEY:latest \
  --set-secrets=TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest \
  --set-secrets=TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest \
  --set-secrets=TWILIO_WHATSAPP_FROM=TWILIO_WHATSAPP_FROM:latest \
  --set-secrets=TWILIO_WHATSAPP_JOIN_CODE=TWILIO_WHATSAPP_JOIN_CODE:latest \
--set-secrets=ROUNDTRIP_TOKEN=ROUNDTRIP_TOKEN:latest \
  --set-secrets=WEB_PASSCODE=WEB_PASSCODE:latest \
  --set-secrets=WEB_PASSCODE_SALT=WEB_PASSCODE_SALT:latest \
  --set-secrets=CUTOFF_SECRET=CUTOFF_SECRET:latest \
  --project="$PROJECT_ID"

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
  --region="$REGION" --platform=managed --project="$PROJECT_ID" \
  --format='value(status.url)')"
log "Service live at $SERVICE_URL"

log "Granting $PUSH_SA -> Cloud Run invoker on $SERVICE"
gcloud run services add-iam-policy-binding "$SERVICE" \
  --region="$REGION" --platform=managed \
  --member="serviceAccount:${PUSH_SA_EMAIL}" \
  --role=roles/run.invoker --project="$PROJECT_ID" >/dev/null

# The Cloud Scheduler -> Pub/Sub publisher binding plus the daily Cutoff job
# (issue #9). The job publishes a message whose body carries the CUTOFF_SECRET
# so the (unauthenticated-by-deploy) /api/cutoff endpoint can verify it; the
# push subscription then delivers the envelope to Cloud Run on schedule.

log "Granting $SCHED_SA -> Pub/Sub publisher on $TOPIC"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SCHED_SA_EMAIL}" \
  --role=roles/pubsub.publisher --condition=None >/dev/null

log "Creating Cloud Scheduler job (daily Cutoff -> topic -> Cloud Run)"
if ! gcloud scheduler jobs describe "${TOPIC}-job" \
  --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  cutoff_secret="$(gcloud secrets versions access latest \
    --secret=CUTOFF_SECRET --project="$PROJECT_ID")"
  [[ -n "$cutoff_secret" ]] || \
    error "CUTOFF_SECRET secret must have a value to create the scheduler job"
  SCHED_MESSAGE="$(python3 -c "import json,sys;print(json.dumps({'secret':sys.argv[1]}))" "$cutoff_secret")"
  gcloud scheduler jobs create pubsub "${TOPIC}-job" \
    --location="$REGION" \
    --schedule="$SCHEDULE" \
    --time-zone="$SCHEDULE_TZ" \
    --topic="$TOPIC" \
    --message-body="$SCHED_MESSAGE" \
    --oidc-service-account-email="$SCHED_SA_EMAIL" \
    --project="$PROJECT_ID"
  unset cutoff_secret SCHED_MESSAGE
else
  echo "Scheduler job ${TOPIC}-job already exists."
fi

log "Attaching the Cutoff push subscription endpoint (path lands with ticket 8)"
if ! gcloud pubsub subscriptions describe "${TOPIC}-sub" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud pubsub subscriptions create "${TOPIC}-sub" \
    --topic="$TOPIC" \
    --project="$PROJECT_ID" \
    --push-endpoint="${SERVICE_URL}/api/cutoff" \
    --push-auth-service-account="$PUSH_SA_EMAIL"
else
  echo "Subscription ${TOPIC}-sub already exists."
fi

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
echo "  2. POST /api/roundtrip to exercise the deployed agent (message in -> reply out)."
echo "  3. Push to main now auto-deploys via the Cloud Build trigger."
