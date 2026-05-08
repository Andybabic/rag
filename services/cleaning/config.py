from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    USE_MINERU: bool = False
    MINERU_API_URL: str = "http://mineru:8000"
    MINERU_PAGE_BY_PAGE: bool = True

    BATCH_PARSE_WORKERS: int = 2
    BATCH_CHUNK_WORKERS: int = 4

    MAX_RETRIES: int = 3
    RETRY_BACKOFF_BASE: int = 5

    # ── LLM provider pipeline (alt-text generation) ─────────
    VISION_PROVIDER: str = "ollama"  # ollama | openai
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_API_KEY: str | None = None
    VISION_MODEL: str = "qwen2.5:14b"

    # File storage for original documents
    FILE_STORAGE_DIR: str = "/data/documents"

    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./logs"

    model_config = {"env_prefix": ""}


settings = Settings()
