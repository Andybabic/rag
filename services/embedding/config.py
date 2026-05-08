from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── LLM provider pipeline ────────────────────────────────
    EMBEDDING_PROVIDER: str = "ollama"  # ollama | openai
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_API_KEY: str | None = None

    # ── Embedding settings ───────────────────────────────────
    EMBEDDING_MODEL: str = "bge-m3"
    EMBED_BATCH_SIZE: int = 50

    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": ""}


settings = Settings()
