# Built from the repository root:
#   docker build -f services/api/Dockerfile -t nightshift-api .
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
COPY services/api/requirements.txt services/api/requirements.txt
RUN pip install -r services/api/requirements.txt

COPY packages/ packages/
COPY services/api/ services/api/

USER 1000
EXPOSE 8080
# Cloud Run tells the container which port to listen on and is entitled to
# change it; a hardcoded 8080 works right up until it does not, and the
# symptom is a revision that never passes its startup probe.
CMD exec uvicorn services.api.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8080}"
