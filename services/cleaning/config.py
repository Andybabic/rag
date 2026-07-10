from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    USE_MINERU: bool = False
    MINERU_API_URL: str = "http://mineru:8000"
    MINERU_PAGE_BY_PAGE: bool = True  # deprecated: mineru-api paginates itself
    # mineru-api /file_parse backend. "pipeline" = traditional multi-model
    # pipeline (OCR + table recognition, multilingual) – matches the proven
    # dashboard config for scanned German regulation PDFs.
    MINERU_BACKEND: str = "pipeline"
    # Synchronous /file_parse on a scanned multi-page PDF (OCR + tables) can
    # take minutes – generous per-request timeout, not the 120s default.
    MINERU_TIMEOUT: float = 900.0
    # Large PDFs can exhaust MinerU (timeout / OOM) in a single call. When a PDF
    # has more pages than this, it is split into parts of this many pages each,
    # parsed separately, and merged back into one document (page numbers,
    # anchors and images are re-based so the result is identical to a single
    # pass). Set to 0 to disable splitting.
    MINERU_MAX_PAGES_PER_CHUNK: int = 50

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
    # How many image alt-text (vision) calls run concurrently during ingestion.
    # Higher = faster for image-heavy PDFs, but more load on the vision backend.
    VISION_CONCURRENCY: int = 4

    # ── Per-usecase config DB (optional; falls back to env if unset) ──
    DATABASE_URL: str | None = None

    # File storage for original documents
    FILE_STORAGE_DIR: str = "/data/documents"

    # When true, every MinerU image is persisted alongside a sidecar
    # <image_id>.txt containing the generated alt-text + the surrounding
    # document context that was sent to the vision LLM. Off by default;
    # turn on for tuning the vision prompt / spotting bad descriptions.
    DEBUG_IMAGE_CAPTIONS: bool = False

    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./logs"

    model_config = {"env_prefix": ""}


settings = Settings()
