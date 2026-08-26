from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── LLM provider pipeline ────────────────────────────────
    LLM_PROVIDER: str = "ollama"  # ollama | openai
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_API_KEY: str | None = None

    # ── Service settings ─────────────────────────────────────
    LLM_MODEL: str = "qwen2.5:14b"
    EMBEDDING_SERVICE_URL: str
    VECTORDB_SERVICE_URL: str
    DATABASE_URL: str
    AGENT_MAX_STEPS: int = 5
    MEMORY_MAX_CHARS: int = 4000
    # Wall-clock budget (seconds) for one sub-agent inside the manager fan-out.
    # The fan-out is a gather, so without a per-agent deadline the slowest
    # specialist sets the phase duration even when it is stuck. Generous
    # enough that a healthy multi-step agent is never cut off.
    SUBAGENT_TIMEOUT_S: float = 150.0
    # A query with at most this many words is treated as vague/incomplete, so
    # the manager recalls relevant past Q&A from the memory RAG to plan more
    # specific sub-tasks.
    VAGUE_QUERY_MAX_WORDS: int = 6
    LOG_LEVEL: str = "INFO"

    # ── Auth bootstrap ───────────────────────────────────────
    # The first admin is seeded from these on startup if the users table is
    # empty. Leave unset to skip seeding (e.g. once an admin already exists).
    ADMIN_USERNAME: str | None = None
    ADMIN_PASSWORD: str | None = None

    model_config = {"env_prefix": ""}


settings = Settings()
