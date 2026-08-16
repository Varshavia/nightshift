# Built from the repository root:
#   docker build -f services/scanner/Dockerfile -t nightshift-scanner .
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
COPY services/scanner/requirements.txt services/scanner/requirements.txt
RUN pip install -r services/scanner/requirements.txt

COPY packages/ packages/
COPY services/scanner/ services/scanner/

# Runs as a Cloud Run Job: publishes work, then exits.
USER 1000
ENTRYPOINT ["python", "-m", "services.scanner.main"]
