from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    OLLAMA_BASE_URL: str
    EMBEDDING_MODEL: str = "bge-m3"
    EMBED_BATCH_SIZE: int = 50
    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": ""}


settings = Settings()
