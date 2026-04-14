from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": ""}


settings = Settings()
