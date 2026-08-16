#!/usr/bin/env bash
#
# Idempotent GCP deployment. Safe to re-run; every step either creates the
# resource or confirms it already exists. Least privilege throughout: each
# service account gets exactly the roles its service uses and no project-level
# editor role anywhere.
#
#   ./infra/deploy.sh            deploy everything
#   ./infra/deploy.sh scanner    deploy one service
#
set -euo pipefail

PROJECT="${NIGHTSHIFT_GCP_PROJECT:?set NIGHTSHIFT_GCP_PROJECT}"
REGION="${NIGHTSHIFT_GCP_REGION:-us-central1}"
TOPIC="${NIGHTSHIFT_JOBS_TOPIC:-nightshift-jobs}"
REPO="nightshift"
TARGET="${1:-all}"

say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------------- #
# APIs
# --------------------------------------------------------------------------- #
say "enabling APIs"
gcloud services enable \
  run.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  firestore.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  --project "$PROJECT" --quiet

# --------------------------------------------------------------------------- #
# Service accounts — one per service, each with only what it uses
# --------------------------------------------------------------------------- #
create_sa() {
  local name="$1" display="$2"
  if ! gcloud iam service-accounts describe "${name}@${PROJECT}.iam.gserviceaccount.com" \
      --project "$PROJECT" >/dev/null 2>&1; then
    say "creating service account ${name}"
    gcloud iam service-accounts create "$name" --display-name "$display" --project "$PROJECT"
  fi
}

grant() {
  local name="$1" role="$2"
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${name}@${PROJECT}.iam.gserviceaccount.com" \
    --role "$role" --condition=None --quiet >/dev/null
}

create_sa nightshift-scanner "Nightshift scanner"
create_sa nightshift-worker  "Nightshift worker"
create_sa nightshift-api     "Nightshift read API"

# The scanner publishes and writes job documents. It never calls Gemini.
grant nightshift-scanner roles/pubsub.publisher
grant nightshift-scanner roles/datastore.user
grant nightshift-scanner roles/aiplatform.user       # Gemma triage only

# The worker consumes, checkpoints and calls Gemini. It cannot publish new jobs.
grant nightshift-worker roles/pubsub.subscriber
grant nightshift-worker roles/datastore.user
grant nightshift-worker roles/aiplatform.user

# The API reads. Deliberately no datastore.owner — approvals write one field
# through a narrow path, not a broad role.
grant nightshift-api roles/datastore.viewer

# --------------------------------------------------------------------------- #
# Firestore, Pub/Sub, Artifact Registry
# --------------------------------------------------------------------------- #
if ! gcloud firestore databases describe --database="(default)" --project "$PROJECT" >/dev/null 2>&1; then
  say "creating Firestore database"
  gcloud firestore databases create --location="$REGION" --project "$PROJECT" --quiet
fi

if ! gcloud pubsub topics describe "$TOPIC" --project "$PROJECT" >/dev/null 2>&1; then
  say "creating Pub/Sub topic ${TOPIC}"
  gcloud pubsub topics create "$TOPIC" --project "$PROJECT"
fi

if ! gcloud artifacts repositories describe "$REPO" \
    --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  say "creating Artifact Registry repository"
  gcloud artifacts repositories create "$REPO" \
    --repository-format=docker --location "$REGION" --project "$PROJECT"
fi

IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"

# --------------------------------------------------------------------------- #
# Build and deploy
# --------------------------------------------------------------------------- #
build_and_push() {
  local service="$1"
  say "building ${service}"
  gcloud builds submit --project "$PROJECT" \
    --tag "${IMAGE_BASE}/${service}:latest" \
    --config /dev/null . -f "services/${service}/Dockerfile" 2>/dev/null \
    || docker build -f "services/${service}/Dockerfile" -t "${IMAGE_BASE}/${service}:latest" . \
    && docker push "${IMAGE_BASE}/${service}:latest"
}

deploy_job() {
  local service="$1" sa="$2" timeout="$3"
  build_and_push "$service"
  say "deploying job ${service}"
  gcloud run jobs deploy "nightshift-${service}" \
    --image "${IMAGE_BASE}/${service}:latest" \
    --region "$REGION" --project "$PROJECT" \
    --service-account "${sa}@${PROJECT}.iam.gserviceaccount.com" \
    --task-timeout "$timeout" \
    --max-retries 1 \
    --set-env-vars "NIGHTSHIFT_GCP_PROJECT=${PROJECT},NIGHTSHIFT_GCP_REGION=${REGION},NIGHTSHIFT_JOBS_TOPIC=${TOPIC},ALLOW_UPSTREAM_PRS=false" \
    --set-secrets "GITHUB_TOKEN=nightshift-github-token:latest" \
    --quiet
}

case "$TARGET" in
  scanner|all) deploy_job scanner nightshift-scanner 900s ;;&
  worker|all)  deploy_job worker  nightshift-worker  1800s ;;&
  api|all)
    build_and_push api
    say "deploying api"
    gcloud run deploy nightshift-api \
      --image "${IMAGE_BASE}/api:latest" \
      --region "$REGION" --project "$PROJECT" \
      --service-account "nightshift-api@${PROJECT}.iam.gserviceaccount.com" \
      --no-allow-unauthenticated \
      --set-env-vars "NIGHTSHIFT_GCP_PROJECT=${PROJECT}" \
      --quiet
    ;;
esac

# --------------------------------------------------------------------------- #
# Nightly schedule — 02:00 Europe/Istanbul
# --------------------------------------------------------------------------- #
if ! gcloud scheduler jobs describe nightshift-nightly \
    --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  say "creating nightly schedule"
  gcloud scheduler jobs create http nightshift-nightly \
    --location "$REGION" --project "$PROJECT" \
    --schedule "0 2 * * *" --time-zone "Europe/Istanbul" \
    --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/nightshift-scanner:run" \
    --http-method POST \
    --oauth-service-account-email "nightshift-scanner@${PROJECT}.iam.gserviceaccount.com"
fi

say "done — nightshift deployed to ${PROJECT}/${REGION}"
