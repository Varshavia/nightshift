# Built from the repository root:
#   docker build -f services/worker/Dockerfile -t nightshift-worker .
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/packages

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

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
USER nightshift
ENTRYPOINT ["python", "-m", "services.worker.main"]
