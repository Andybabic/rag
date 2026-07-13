from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    # Per-request timeout (s) for the Qdrant client. The default (~5s) is far
    # too short for a large upsert (a 150-page PDF yields many 4096-dim
    # vectors), which then fails with "timed out".
    QDRANT_TIMEOUT: float = 120.0
    # Upsert in batches so one big document doesn't send a single huge request
    # that stalls/times out; each batch is a bounded, retryable unit.
    UPSERT_BATCH_SIZE: int = 128
    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": ""}


settings = Settings()
