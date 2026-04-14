FROM python:3.11-slim
WORKDIR /tests

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY services/integration/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/integration/ .

CMD ["pytest", "-v", "--tb=short", "test_e2e.py"]
