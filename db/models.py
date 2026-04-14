"""SQLAlchemy ORM models – single source of truth for Alembic autogenerate."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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
