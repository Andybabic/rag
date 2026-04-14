from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    OLLAMA_BASE_URL: str
    LLM_MODEL: str = "qwen2.5:14b"
    EMBEDDING_SERVICE_URL: str
    VECTORDB_SERVICE_URL: str
    DATABASE_URL: str
    AGENT_MAX_STEPS: int = 5
    MEMORY_MAX_CHARS: int = 4000
    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": ""}


settings = Settings()
