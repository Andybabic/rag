"""Benchmark settings: benchmark/.env → process env → CLI flags (last wins)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR.parent
CACHE_DIR = BENCH_DIR / ".cache"
RUNS_DIR = BENCH_DIR / "runs"


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser – no python-dotenv dependency for one file."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass
class Settings:
    # Empty → found automatically (localhost:5173, then :3000).
    frontend_url: str = ""
    evaluation_url: str = "http://localhost:8005"
    vectordb_url: str = "http://localhost:8004"
    cleaning_url: str = "http://localhost:8001"
    username: str = ""
    password: str = ""

    judge_provider: str = "openai"
    judge_base_url: str = ""
    judge_api_key: str = ""
    judge_model: str = "gpt-4o-mini"
    judge_timeout: float = 120.0
    judge_num_ctx: int = 32768
    judge_concurrency: int = 4

    split: str = "test"
    mono: int = 100
    multi: int = 50
    negative: int = 10
    # Distracting articles per question in the index; -1 = all of them.
    distractors: int = -1
    variants: list[str] = field(default_factory=lambda: ["de", "en", "multi", "neg"])
    seed: int = 42

    use_case: str = "xrag_bench"
    ingest_concurrency: int = 4
    query_concurrency: int = 2
    query_timeout: float = 900.0

    @property
    def judge_enabled(self) -> bool:
        return bool(self.judge_model)


# Settings field → environment variable.
_ENV_KEYS = {
    "frontend_url": "KERMIT_FRONTEND_URL",
    "evaluation_url": "KERMIT_EVALUATION_URL",
    "vectordb_url": "KERMIT_VECTORDB_URL",
    "cleaning_url": "KERMIT_CLEANING_URL",
    "username": "KERMIT_USERNAME",
    "password": "KERMIT_PASSWORD",
    "judge_provider": "BENCH_JUDGE_PROVIDER",
    "judge_base_url": "BENCH_JUDGE_BASE_URL",
    "judge_api_key": "BENCH_JUDGE_API_KEY",
    "judge_model": "BENCH_JUDGE_MODEL",
    "judge_timeout": "BENCH_JUDGE_TIMEOUT",
    "judge_num_ctx": "BENCH_JUDGE_NUM_CTX",
    "judge_concurrency": "BENCH_JUDGE_CONCURRENCY",
    "split": "XRAG_SPLIT",
    "mono": "BENCH_MONO",
    "multi": "BENCH_MULTI",
    "negative": "BENCH_NEGATIVE",
    "distractors": "BENCH_DISTRACTORS",
    "variants": "BENCH_VARIANTS",
    "seed": "BENCH_SEED",
    "use_case": "BENCH_USE_CASE",
    "ingest_concurrency": "BENCH_INGEST_CONCURRENCY",
    "query_concurrency": "BENCH_QUERY_CONCURRENCY",
    "query_timeout": "BENCH_QUERY_TIMEOUT",
}


_EMPTY_ALLOWED = {"judge_model", "judge_api_key", "username", "password"}


def _coerce(current, raw: str):
    if isinstance(current, bool):
        return raw.lower() in {"1", "true", "yes"}
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [v.strip() for v in raw.split(",") if v.strip()]
    return raw


def load_settings(overrides: dict | None = None) -> Settings:
    env = read_env_file(BENCH_DIR / ".env")
    env.update({k: v for k, v in os.environ.items() if k in _ENV_KEYS.values()})

    settings = Settings()
    for f in fields(settings):
        key = _ENV_KEYS.get(f.name)
        if not key or key not in env:
            continue
        current = getattr(settings, f.name)
        # Empty means "default" – except where empty is a deliberate choice:
        # "BENCH_JUDGE_MODEL=" turns the judge off.
        if env[key] != "" or f.name in _EMPTY_ALLOWED:
            setattr(settings, f.name, _coerce(current, env[key]))
    for name, value in (overrides or {}).items():
        if value is not None:
            setattr(settings, name, value)

    # Same admin the stack seeds itself with, unless the benchmark has its own.
    if not settings.username or not settings.password:
        root_env = read_env_file(REPO_DIR / ".env")
        settings.username = settings.username or root_env.get("ADMIN_USERNAME", "")
        settings.password = settings.password or root_env.get("ADMIN_PASSWORD", "")
    return settings
