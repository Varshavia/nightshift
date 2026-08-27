# Built from the repository root:
#   docker build -f services/worker/Dockerfile -t nightshift-worker .
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/packages

WORKDIR /app

# `git` and `ca-certificates` are ours. Everything after is somebody else's
# build dependency, and the list comes from a measurement rather than a guess:
# PowerDNS-Admin — a Flask application with 43 pinned dependencies and a real
# suite — was recorded UNBUILDABLE solely because `pkg-config --exists
# mysqlclient` failed. An image without these libraries does not measure how
# much of the ecosystem the fleet can repair; it measures its own contents, and
# every repository it cannot build is one it quietly declines to help.
#
# Deliberately identical to infra/probe.Dockerfile. The probe exists to predict
# what the fleet will do, and it cannot do that from a richer environment than
# the fleet has.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates \
      build-essential pkg-config \
      default-libmysqlclient-dev \
      libpq-dev \
      libxml2-dev libxslt1-dev \
      libffi-dev libssl-dev \
      libjpeg-dev zlib1g-dev \
      libldap2-dev libsasl2-dev \
 && rm -rf /var/lib/apt/lists/*

# `uv`, for one job: fetching the interpreter a repository asked for. The image
# ships 3.12 and a great many projects pinned before 2023 cannot be installed
# with it — 3.12 removed `distutils`, and their `setup.py` imports it. Offering
# every repository the same Python and recording UNBUILDABLE is a measurement of
# this container, not of them. See services/worker/interpreter.py.
#
# Copied from the published image rather than curl-piped into a shell: the
# deployment already trusts a registry, and it should not also have to trust a
# script fetched at build time.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# Dependencies first so that a code change does not invalidate the layer.
COPY services/worker/requirements.txt services/worker/requirements.txt
RUN pip install -r services/worker/requirements.txt

COPY packages/ packages/
COPY services/worker/ services/worker/
COPY templates/ templates/

# The agent's sandbox is this container. It runs unprivileged and its writes are
# confined to /workspace by the policy engine as well as by the filesystem.
RUN useradd --create-home --uid 1000 nightshift \
 && mkdir -p /workspace && chown nightshift:nightshift /workspace

# Declared next to the directory it names, rather than passed by the deployment
# script. `--set-env-vars NIGHTSHIFT_WORKSPACE_ROOT=/workspace` reached Cloud Run
# as `C:/Program Files/Git/workspace`, because Git Bash rewrites POSIX-looking
# arguments on the way out and gcloud has no reason to doubt what it is handed.
# Every job then failed trying to mkdir a drive letter inside a Linux container.
# A path that only means anything inside this image should be a fact of the
# image, where no shell on anybody's laptop gets a vote.
ENV NIGHTSHIFT_WORKSPACE_ROOT=/workspace

USER nightshift
ENTRYPOINT ["python", "-m", "services.worker.main"]
