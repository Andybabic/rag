"""SQLAlchemy ORM models – single source of truth for Alembic autogenerate."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Query(Base):
    __tablename__ = "queries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    use_case = Column(String(50), nullable=False)
    session_id = Column(UUID(as_uuid=True))
    role = Column(String(20), server_default="default")
    query_text = Column(Text, nullable=False)
    answer_text = Column(Text)
    chunks_used = Column(JSONB)
    agent_steps = Column(JSONB)
    scores = Column(JSONB)
    sufficient = Column(Boolean)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_queries_use_case", "use_case"),
        Index("idx_queries_created_at", "created_at"),
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_id = Column(UUID(as_uuid=True), ForeignKey("queries.id"))
    rating = Column(String(10))
    comment = Column(Text)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "rating IN ('positive', 'negative')",
            name="ck_feedback_rating",
        ),
        Index("idx_feedback_query_id", "query_id"),
    )


class IngestionLog(Base):
    __tablename__ = "ingestion_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_name = Column(String(500), nullable=False)
    file_hash = Column(String(64), nullable=False, unique=True)
    collection = Column(String(100))
    use_case = Column(String(50))
    chunk_count = Column(Integer)
    status = Column(String(20), server_default="ok")
    error_detail = Column(Text)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_ingestion_file_hash", "file_hash"),
    )


class AgentMemory(Base):
    __tablename__ = "agent_memory"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    use_case = Column(String(50), nullable=False, unique=True)
    memory_text = Column(Text)
    version = Column(Integer, server_default="1")
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_id = Column(UUID(as_uuid=True), ForeignKey("queries.id"))
    reranked_chunks = Column(JSONB)
    threshold_passed = Column(Boolean)
    refined_queries = Column(JSONB)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )


class UseCaseConfig(Base):
    """Per-usecase override of LLM provider settings.

    Any column NULL = "fall back to .env value at call time".
    API keys are stored encrypted (Fernet, see shared.security.crypto).
    """

    __tablename__ = "usecase_config"

    use_case = Column(String(50), primary_key=True)
    chat_provider = Column(String(20))
    embedding_provider = Column(String(20))
    vision_provider = Column(String(20))
    ollama_base_url = Column(Text)
    ollama_api_key_encrypted = Column(Text)
    openai_base_url = Column(Text)
    openai_api_key_encrypted = Column(Text)
    llm_model = Column(String(100))
    embedding_model = Column(String(100))
    vision_model = Column(String(100))
    embedding_dimension = Column(Integer)
    temperature = Column(Float)
    max_tokens = Column(Integer)
    embed_batch_size = Column(Integer)
    agent_max_steps = Column(Integer)
    memory_max_chars = Column(Integer)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )


class UseCasePrompt(Base):
    """Editable prompt overrides keyed by (use_case, prompt_key).

    Use ``use_case = '*'`` for global prompts (e.g. agent.react_suffix).
    Common keys:
      - agent.system.{role}            (per-usecase agent system prompt)
      - agent.react_suffix             (use_case='*' — global ReAct suffix)
      - vision.alt_text                (cleaning vision prompt)
    """

    __tablename__ = "usecase_prompts"

    use_case = Column(String(50), nullable=False)
    prompt_key = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("use_case", "prompt_key", name="pk_usecase_prompts"),
    )


class UseCaseSkill(Base):
    """Per-usecase Skill / Regel — two-text module appended to the agent prompt.

    ``overview`` is a short identifier shown in the UI; ``detailed_task`` is
    the actual instruction text that the LLM follows. Enabled skills are
    concatenated into the agent's system prompt in ``position`` order.
    """

    __tablename__ = "usecase_skills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    use_case = Column(String(50), nullable=False)
    name = Column(String(100), nullable=False)
    overview = Column(Text, nullable=False)
    detailed_task = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, server_default="true")
    position = Column(Integer, nullable=False, server_default="0")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_usecase_skills_use_case", "use_case"),
        Index("uq_usecase_skills_name", "use_case", "name", unique=True),
    )
