# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

# Create the non-root user up front so the later COPY --chown is cheap and we
# avoid a recursive chown pass over the whole app dir.
# The cache dir is created here, owned by appuser, because a named volume
# mounted onto a path that does not exist in the image is initialised by Docker
# as root — and the container runs as appuser, which then cannot write it. The
# evaluation service mounts its model cache here; without this the reranker
# dies at startup with EACCES and the whole system runs in degraded retrieval
# with no symptom beyond worse answers.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /data/documents /home/appuser/.cache \
    && chown -R appuser /data /home/appuser/.cache

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install shared package first (changes less often → better layer caching).
# pip's download cache lives on a BuildKit cache mount, so repeated builds reuse
# already-downloaded wheels instead of re-fetching them.
COPY shared/ /shared
RUN --mount=type=cache,target=/root/.cache/pip pip install -e /shared

# Service-specific dependencies. This layer is cached until requirements.txt
# changes — editing code below does NOT reinstall dependencies.
ARG SERVICE_DIR=.
COPY ${SERVICE_DIR}/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# Copy service code last, already owned by appuser (no extra recursive chown).
COPY --chown=appuser ${SERVICE_DIR}/ .

USER appuser

ARG SERVICE_PORT=8000
EXPOSE $SERVICE_PORT

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
