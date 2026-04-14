from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DEFAULT_CHUNK_SIZE: int = 512
    DEFAULT_CHUNK_OVERLAP: int = 64
    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": ""}


settings = Settings()
