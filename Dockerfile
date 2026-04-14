FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install shared package first (changes less often → better layer caching)
COPY shared/ /shared
RUN pip install --no-cache-dir -e /shared

# Install service-specific dependencies
ARG SERVICE_DIR=.
COPY ${SERVICE_DIR}/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY ${SERVICE_DIR}/ .

# Run as non-root user
RUN useradd -m -u 1000 appuser \
    && chown -R appuser /app \
    && mkdir -p /data/documents && chown -R appuser /data
USER appuser

ARG SERVICE_PORT=8000
EXPOSE $SERVICE_PORT

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
