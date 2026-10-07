"""LLM judge (OpenAI-compatible or Ollama) – grades Kermit's answer against the gold answer.

Two steps:
1. ``extract_facts``: split the gold answer into atomic facts. Depends only
   on the dataset, so it is cached per judge model across runs.
2. ``grade``: check the answer against question, gold answer and facts.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx
from settings import CACHE_DIR, Settings

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
# Reasoning models (Qwen3, DeepSeek-R1 …) may prepend their thoughts inline.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

DEFAULT_BASE_URL = {"openai": "https://api.openai.com/v1", "ollama": "http://localhost:11434"}

FACTS_PROMPT = """Split the reference answer into the minimal list of atomic facts \
a correct answer MUST contain to answer the question (names, numbers, dates, \
the outcome of a comparison, the members of a set). Leave out filler and context.
Keep the language of the reference answer.

Question: {question}
Reference answer: {gold}

Reply with JSON only: {{"facts": ["...", "..."]}}"""

GRADE_PROMPT = """You grade the answer of a retrieval-augmented assistant.

Question: {question}
Reference answer: {gold}
Required facts:
{facts}

Assistant answer (citation markers like [1] removed):
\"\"\"{answer}\"\"\"

The assistant answer may be in another language than the reference; judge \
meaning, not wording. Decide:
- facts_present: for EACH required fact (same order) whether the answer states \
it correctly (true/false).
- verdict: "correct" (answers the question, all essential facts right), \
"partial" (some essential facts right, some missing or wrong), "incorrect" \
(wrong, contradicts the reference, or does not answer).
- refused: true if the assistant says it cannot answer / found no information.
- extra_claims: number of statements NOT needed to answer the question \
(background, side facts, repetition).
- contradictions: number of statements that contradict the reference answer.

Reply with JSON only:
{{"verdict": "correct|partial|incorrect", "refused": false, \
"facts_present": [true, false], "extra_claims": 0, "contradictions": 0, \
"reason": "one sentence"}}"""

REFUSAL_PROMPT = """The assistant was asked a question whose answer is NOT \
available in its document collection. The correct behaviour is to say that the \
documents do not contain the answer.

Question: {question}
Assistant answer:
\"\"\"{answer}\"\"\"

Reply with JSON only: {{"refused": true, "hallucinated": false, \
"reason": "one sentence"}}
refused: the assistant says it cannot answer / found nothing relevant.
hallucinated: the assistant nevertheless presents a concrete answer as fact."""

PING_PROMPT = 'Connection test. Reply with exactly this JSON and nothing else: {"ok": true}'

SYSTEM_PROMPT = "You are a strict, careful evaluator. Output JSON only."

VERDICT_SCORE = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}


class JudgeError(RuntimeError):
    pass


class JudgeConfigError(JudgeError):
    """The judge endpoint rejects every request – stop instead of retrying."""


class Judge:
    def __init__(self, settings: Settings):
        self.provider = settings.judge_provider.lower()
        if self.provider not in DEFAULT_BASE_URL:
            raise JudgeConfigError(
                f"BENCH_JUDGE_PROVIDER={settings.judge_provider!r} – erlaubt: openai, ollama"
            )
        self.model = settings.judge_model
        self.num_ctx = settings.judge_num_ctx
        base = (settings.judge_base_url or DEFAULT_BASE_URL[self.provider]).rstrip("/")
        headers = {"content-type": "application/json"}
        if self.provider == "ollama":
            # Native API, like Kermit's own Ollama provider; an OpenAI-style
            # ".../v1" base is tolerated.
            self.url = base.removesuffix("/api/chat").removesuffix("/v1") + "/api/chat"
            if settings.judge_api_key:
                headers["X-API-Key"] = settings.judge_api_key
        else:
            # Part or all of the endpoint path in BENCH_JUDGE_BASE_URL is a
            # common slip ("…/chat", "…/chat/completions"); keep only the base.
            self.url = base.removesuffix("/completions").removesuffix("/chat") + "/chat/completions"
            if settings.judge_api_key:
                headers["authorization"] = f"Bearer {settings.judge_api_key}"
        self.client = httpx.AsyncClient(headers=headers, timeout=settings.judge_timeout)
        self.sem = asyncio.Semaphore(settings.judge_concurrency)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.model)
        self.facts_cache_path = CACHE_DIR / f"facts.{safe}.json"
        self.facts_cache: dict[str, list[str]] = _load_json(self.facts_cache_path)

    async def aclose(self) -> None:
        await self.client.aclose()
        _save_json(self.facts_cache_path, self.facts_cache)

    def _payload(self, prompt: str) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        if self.provider == "ollama":
            return {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": "json",
                "options": {"temperature": 0, "num_ctx": self.num_ctx},
            }
        return {"model": self.model, "temperature": 0, "messages": messages}

    def _content(self, data: dict) -> str:
        message = data["message"] if self.provider == "ollama" else data["choices"][0]["message"]
        return message.get("content") or ""

    async def _ask_json(self, prompt: str, *, attempts: int = 3) -> dict:
        last = ""
        for attempt in range(attempts):
            async with self.sem:
                try:
                    resp = await self.client.post(self.url, json=self._payload(prompt))
                    if resp.status_code in (400, 401, 403, 404):
                        # Wrong key, model or URL – retrying cannot help.
                        raise JudgeConfigError(
                            f"Judge {self.url} antwortet HTTP {resp.status_code}: {resp.text[:200]}"
                        )
                    resp.raise_for_status()
                    content = self._content(resp.json())
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
            match = _JSON_RE.search(_THINK_RE.sub("", content))
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            last = f"keine gültige JSON-Antwort: {content[:200]!r}"
        raise JudgeError(last)

    async def ping(self) -> float:
        """One tiny request: URL, key, model name and JSON output in one go.

        Returns the round trip in seconds; raises JudgeError otherwise.
        """
        t0 = time.monotonic()
        data = await self._ask_json(PING_PROMPT, attempts=1)
        if data.get("ok") is not True:
            raise JudgeError(f"unerwartete Antwort auf den Verbindungstest: {data!r}")
        return time.monotonic() - t0

    async def extract_facts(self, cache_key: str, question: str, gold: str) -> list[str]:
        if cache_key in self.facts_cache:
            return self.facts_cache[cache_key]
        data = await self._ask_json(FACTS_PROMPT.format(question=question, gold=gold))
        facts = [str(f).strip() for f in data.get("facts", []) if str(f).strip()] or [gold]
        self.facts_cache[cache_key] = facts
        return facts

    async def grade(self, question: str, gold: str, facts: list[str], answer: str) -> dict:
        data = await self._ask_json(
            GRADE_PROMPT.format(
                question=question,
                gold=gold,
                facts="\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1)),
                answer=answer,
            )
        )
        verdict = str(data.get("verdict", "incorrect")).lower()
        if verdict not in VERDICT_SCORE:
            verdict = "incorrect"
        present = [bool(p) for p in data.get("facts_present", [])][: len(facts)]
        return {
            "judge_verdict": verdict,
            "judge_score": VERDICT_SCORE[verdict],
            "judge_refused": bool(data.get("refused", False)),
            "fact_recall": sum(present) / len(facts) if facts else None,
            "facts": facts,
            "facts_present": present,
            "extra_claims": _as_int(data.get("extra_claims")),
            "contradictions": _as_int(data.get("contradictions")),
            "judge_reason": str(data.get("reason", "")),
        }

    async def grade_refusal(self, question: str, answer: str) -> dict:
        data = await self._ask_json(REFUSAL_PROMPT.format(question=question, answer=answer))
        return {
            "judge_refused": bool(data.get("refused", False)),
            "hallucinated": bool(data.get("hallucinated", False)),
            "judge_reason": str(data.get("reason", "")),
        }


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
