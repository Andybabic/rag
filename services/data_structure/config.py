from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Chunk-Groesse und -Ueberlappung in TOKENS (LlamaIndex SentenceSplitter,
    # nicht Zeichen). 1 DE-Token ≈ 3–4 Zeichen → 256 Tokens ≈ 1000 Zeichen.
    # Auf "eine semantische Einheit pro Chunk" getuned, damit kleine
    # Paragraf-Sections nicht von greedy gemergten Nachbarn verschluckt werden.
    DEFAULT_CHUNK_SIZE: int = 256
    DEFAULT_CHUNK_OVERLAP: int = 32
    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": ""}


settings = Settings()
