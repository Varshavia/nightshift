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

# Mirrors .env.example. Set in the environment to override; the defaults are the
# same ones the services fall back to, so a deployment and a local run disagree
# about nothing.
FLEET_POOL="${NIGHTSHIFT_FLEET_POOL:-fleet/pool.json}"
REPAIR_MODEL="${NIGHTSHIFT_REPAIR_MODEL:-gemini-3.5-flash}"
ESCALATION_MODEL="${NIGHTSHIFT_ESCALATION_MODEL:-gemini-3.5-pro}"
FORK_ORG="${NIGHTSHIFT_FORK_ORG:?set NIGHTSHIFT_FORK_ORG — the fleet never operates outside it}"

say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------------- #
# APIs
# --------------------------------------------------------------------------- #
say "enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  firestore.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  cloudtrace.googleapis.com \
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

# The API reads, and writes exactly one thing: an approval. `datastore.viewer`
# cannot write at all and `datastore.user` can write anything, so neither says
# what this service actually does — a custom role does, and it is the difference
# between least privilege as a comment and least privilege as configuration.
if ! gcloud iam roles describe nightshiftApprover --project "$PROJECT" >/dev/null 2>&1; then
  say "creating custom role nightshiftApprover"
  gcloud iam roles create nightshiftApprover --project "$PROJECT" \
    --title "Nightshift control tower" \
    --description "Read jobs; write approvals. Nothing else." \
    --permissions "datastore.entities.get,datastore.entities.list,datastore.entities.create,datastore.entities.update,datastore.entities.delete" \
    --stage GA --quiet
fi
grant nightshift-api "projects/${PROJECT}/roles/nightshiftApprover"

# Everything that runs writes spans, because the cost curve is a query over them
# and a service that cannot write a span drops out of the number silently.
grant nightshift-scanner roles/cloudtrace.agent
grant nightshift-worker  roles/cloudtrace.agent

# Two tokens, because the two services need opposite things from GitHub.
#
# The worker opens pull requests and needs a credential that can write. The
# scanner only reads manifests — but it cannot do that anonymously either: the
# unauthenticated quota is sixty requests an hour, a pool of three hundred
# repositories needs several times that in a single scan, and a refused request
# does not look refused. It comes back as a repository with no tests and no
# pins, which is a wrong answer rather than an error.
#
# So the scanner gets a token with no scopes at all. That is not a smaller
# version of the worker's credential; it is a different kind of thing — one
# that can read public data and cannot write anywhere, by construction rather
# than by policy.
grant nightshift-worker roles/secretmanager.secretAccessor
grant nightshift-scanner roles/secretmanager.secretAccessor

# The scheduler calls the scanner job as this account, so it has to be allowed
# to invoke it. Without this the nightly run fails with a 403 that looks like a
# scheduler problem and is actually an IAM one.
grant nightshift-scanner roles/run.invoker

# Cloud Build runs as the project's default compute service account, and on
# projects created since 2024 that account starts with no roles at all. The
# failure is remote and unhelpful — "the service account running this build does
# not have permission to write logs" — and it happens after the whole repository
# has been uploaded, so it costs a full build cycle to discover.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
say "granting Cloud Build (${BUILD_SA}) what it needs"
for role in roles/logging.logWriter roles/artifactregistry.writer roles/storage.objectUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${BUILD_SA}" --role "$role" \
    --condition=None --quiet >/dev/null
done

# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
if ! gcloud secrets describe nightshift-github-token --project "$PROJECT" >/dev/null 2>&1; then
  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    say "GITHUB_TOKEN is not set and the secret does not exist yet"
    echo "  The worker cannot open a pull request without it. Create it with:" >&2
    echo "    printf %s \"\$GITHUB_TOKEN\" | gcloud secrets create nightshift-github-token \\" >&2
    echo "      --data-file=- --project $PROJECT" >&2
    exit 1
  fi
  say "creating secret nightshift-github-token"
  # Piped rather than passed as an argument: a token on a command line ends up
  # in shell history and in the process list.
  printf %s "$GITHUB_TOKEN" | gcloud secrets create nightshift-github-token \
    --data-file=- --project "$PROJECT" --quiet
fi

if ! gcloud secrets describe nightshift-github-read-token --project "$PROJECT" >/dev/null 2>&1; then
  if [[ -z "${GITHUB_READ_TOKEN:-}" ]]; then
    say "GITHUB_READ_TOKEN is not set and the read-only secret does not exist yet"
    echo "  The scanner reads manifests through the GitHub API. Anonymously that is" >&2
    echo "  sixty requests an hour, which one scan of this pool exceeds several times" >&2
    echo "  over — and a refused request comes back looking like a repository with no" >&2
    echo "  tests rather than like an error." >&2
    echo "" >&2
    echo "  Create a classic token with NO scopes ticked — it can read public data and" >&2
    echo "  write nothing — then:" >&2
    echo "    export GITHUB_READ_TOKEN=ghp_..." >&2
    exit 1
  fi
  say "creating secret nightshift-github-read-token"
  printf %s "$GITHUB_READ_TOKEN" | gcloud secrets create nightshift-github-read-token \
    --data-file=- --project "$PROJECT" --quiet
fi

# Guards the one write the control tower exposes. Generated rather than asked
# for: a key a person invents during a deployment is a key a person reuses.
if ! gcloud secrets describe nightshift-approval-key --project "$PROJECT" >/dev/null 2>&1; then
  say "creating secret nightshift-approval-key"
  python -c "import secrets; print(secrets.token_urlsafe(32), end='')" \
    | gcloud secrets create nightshift-approval-key --data-file=- --project "$PROJECT" --quiet
fi
grant nightshift-api roles/secretmanager.secretAccessor

# --------------------------------------------------------------------------- #
# Firestore, Pub/Sub, Artifact Registry
# --------------------------------------------------------------------------- #
if ! gcloud firestore databases describe --database="(default)" --project "$PROJECT" >/dev/null 2>&1; then
  say "creating Firestore database"
  # `--type` is stated rather than left to the default. A database created in
  # Datastore mode looks fine until the first write, which fails with an error
  # about entity groups that says nothing about the real cause, and the mode
  # cannot be changed afterwards — the database has to be deleted and remade.
  gcloud firestore databases create \
    --location="$REGION" --type=firestore-native --project "$PROJECT" --quiet
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
# Preflight
# --------------------------------------------------------------------------- #
# The scanner's image carries the fork pool, and `load_pool` raises on a missing
# file rather than returning an empty fleet — deliberately, because a scan of
# zero repositories and a quiet night look identical in the morning. Catching it
# here turns that into a message before the build instead of a stack trace at
# two in the morning.
if [[ ! -f "$FLEET_POOL" ]]; then
  say "no fork pool at ${FLEET_POOL}"
  echo "  A fleet with no repositories would run every night and find nothing." >&2
  echo "  Build one first:" >&2
  echo "    python scripts/build_fork_pool.py propose" >&2
  echo "    # read fleet/candidates.json, then" >&2
  echo "    python scripts/build_fork_pool.py fork --from fleet/candidates.json" >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# Build and deploy
# --------------------------------------------------------------------------- #
build_and_push() {
  local service="$1"
  say "building ${service}"
  # Cloud Build with a config file, not `--tag`. The two are mutually exclusive,
  # and `--tag` assumes a Dockerfile at the root of the context — ours are under
  # services/<name>/ but need the root as context to COPY packages/. The previous
  # version of this function combined both flags with a `||` fallback whose
  # precedence meant the local push ran even when the remote build succeeded. It
  # had never been executed.
  gcloud builds submit . \
    --project "$PROJECT" \
    --region "$REGION" \
    --config infra/cloudbuild.yaml \
    --substitutions "_SERVICE=${service},_IMAGE=${IMAGE_BASE}/${service}:latest" \
    --quiet
}

deploy_job() {
  local service="$1" sa="$2" timeout="$3" wants_token="${4:-no}"

  # The GitHub token is attached to the worker and to nothing else. The scanner
  # reads advisories and publishes messages; it has no use for a credential that
  # can write to a repository, and Cloud Run refuses to deploy a job whose
  # service account cannot read a secret it was handed — which is how this was
  # found, on the first real deployment.
  #
  # Granting the scanner `secretAccessor` would have made the error go away and
  # been the wrong fix: least privilege is not a comment in a script, it is
  # which service accounts can read which secrets.
  # Stated either way, never left implicit. `gcloud run jobs deploy` updates an
  # existing job in place and omitting `--set-secrets` does not remove a binding
  # that is already there — it leaves it. So a script that merely stops asking
  # for the secret does not take it away, and the second deployment fails with
  # the identical permission error as the first, which is exactly what happened
  # here. A deployment script has to declare the whole desired state; one that
  # only ever adds is not idempotent, whatever its comments claim.
  local secret_args
  case "$wants_token" in
    write) secret_args=(--set-secrets "GITHUB_TOKEN=nightshift-github-token:latest") ;;
    read)  secret_args=(--set-secrets "GITHUB_TOKEN=nightshift-github-read-token:latest") ;;
    *)     secret_args=(--clear-secrets) ;;
  esac
  build_and_push "$service"
  say "deploying job ${service}"
  gcloud run jobs deploy "nightshift-${service}" \
    --image "${IMAGE_BASE}/${service}:latest" \
    --region "$REGION" --project "$PROJECT" \
    --service-account "${sa}@${PROJECT}.iam.gserviceaccount.com" \
    --task-timeout "$timeout" \
    --max-retries 1 \
    --set-env-vars "^@^NIGHTSHIFT_GCP_PROJECT=${PROJECT}@NIGHTSHIFT_GCP_REGION=${REGION}@NIGHTSHIFT_JOBS_TOPIC=${TOPIC}@NIGHTSHIFT_FLEET_POOL=${FLEET_POOL}@NIGHTSHIFT_WORKSPACE_ROOT=/workspace@NIGHTSHIFT_REPAIR_MODEL=${REPAIR_MODEL}@NIGHTSHIFT_ESCALATION_MODEL=${ESCALATION_MODEL}@NIGHTSHIFT_FORK_ORG=${FORK_ORG}@ALLOW_UPSTREAM_PRS=false" \
    ${secret_args[@]+"${secret_args[@]}"} \
    --quiet
}

case "$TARGET" in
  scanner|all) deploy_job scanner nightshift-scanner 900s read ;;&
  worker|all)  deploy_job worker  nightshift-worker  1800s write ;;&
  api|all)
    build_and_push api
    say "deploying api"
    gcloud run deploy nightshift-api \
      --image "${IMAGE_BASE}/api:latest" \
      --region "$REGION" --project "$PROJECT" \
      --service-account "nightshift-api@${PROJECT}.iam.gserviceaccount.com" \
      --no-allow-unauthenticated \
      --set-env-vars "NIGHTSHIFT_GCP_PROJECT=${PROJECT}" \
      --set-secrets "NIGHTSHIFT_APPROVAL_KEY=nightshift-approval-key:latest" \
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
