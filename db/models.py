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
    UniqueConstraint,
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
    citations = Column(JSONB)
    images = Column(JSONB)
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
    file_hash = Column(String(64), nullable=False)
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
        # Per use case: the same file may be indexed by several use cases.
        UniqueConstraint(
            "use_case", "file_hash", name="uq_ingestion_log_use_case_file_hash"
        ),
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


class UseCase(Base):
    """DB-backed use-case registry — seeded from config/use_cases.json at boot,
    editable via the dashboard afterwards.

    ``id`` is the API id (e.g. ``gw_stpoelten``) used as the ``use_case`` key in
    every other table. ``slug`` is the URL form used by the frontend router.
    Prompts live in :class:`UseCasePrompt` (key ``agent.system.{role}``).
    """

    __tablename__ = "use_cases"

    id = Column(String(50), primary_key=True)
    slug = Column(String(50), nullable=False, unique=True)
    label = Column(String(100), nullable=False)
    description = Column(Text, server_default="")
    color = Column(String(40), server_default="")
    accent = Column(String(20), server_default="")
    roles = Column(JSONB, nullable=False)
    agent_action_names = Column(JSONB, nullable=False)
    default_collection = Column(String(100), nullable=False)
    collection_prefixes = Column(JSONB)
    enabled = Column(Boolean, nullable=False, server_default="true")
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )


class User(Base):
    """Dashboard/app user account. Passwords stored hashed (PBKDF2-SHA256).

    Roles: ``admin`` (manage users + use cases) and ``user`` (chat only).
    The first admin is seeded from env at startup.
    """

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), nullable=False, server_default="user")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'user')",
            name="ck_users_role",
        ),
    )


class DeletedUseCase(Base):
    """Tombstone for a use case deleted via the dashboard.

    Prevents the boot-time seed from resurrecting deleted default use cases.
    Re-creating a use case with the same id removes its tombstone.
    """

    __tablename__ = "deleted_use_cases"

    use_case = Column(String(50), primary_key=True)
    deleted_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserUseCase(Base):
    """Assignment of a user to a use case (many-to-many access control).

    A row means the user may interact with that use case. No rows = no access.
    Admins bypass this and see everything (enforced in the frontend layer).
    """

    __tablename__ = "user_use_cases"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    use_case = Column(
        String(50),
        ForeignKey("use_cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "use_case", name="pk_user_use_cases"),
        Index("idx_user_use_cases_user", "user_id"),
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
    # Plugin layer — a skill may carry exactly one code file (.py or .js,
    # never both). Nullable throughout: a skill without a function part keeps
    # working exactly as before.
    code_language = Column(String(20))
    code_file_name = Column(String(255))
    code = Column(Text)
    # Free-form name/value pairs, defined per skill. Never returned by the
    # regular skill listing — the values leave the DB only inside the
    # execution step and through the admin-only variables endpoint.
    variables = Column(JSONB, nullable=False, server_default="{}")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "code_language IS NULL OR code_language IN ('python', 'javascript')",
            name="ck_usecase_skills_code_language",
        ),
        Index("idx_usecase_skills_use_case", "use_case"),
        Index("uq_usecase_skills_name", "use_case", "name", unique=True),
    )


class UseCaseSkillFunction(Base):
    """One callable function of a skill, derived from its code file.

    The rows are rewritten whenever the skill's code file is saved: the import
    step parses the source and writes what it found. ``ask_for_permission``
    is settled here once, on purpose — the gate that decides whether a user is
    asked before code runs reads this column, never the source text, so the
    marker cannot be changed between the check and the run.

    ``allowed_domains`` empty means no network at all: the runner container is
    started with ``--network none``. Anything listed is reachable only through
    the sandbox's egress proxy, which enforces the same list.
    """

    __tablename__ = "usecase_skill_functions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    skill_id = Column(
        UUID(as_uuid=True),
        ForeignKey("usecase_skills.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(100), nullable=False)
    signature = Column(Text, nullable=False, server_default="")
    description = Column(Text, nullable=False, server_default="")
    params = Column(JSONB, nullable=False, server_default="[]")
    # What the source file declares.
    ask_for_permission = Column(Boolean, nullable=False, server_default="false")
    # What an admin decided in the dashboard; NULL follows the file. Separate
    # because saving a code file rewrites these rows — a single column would
    # silently drop a consent gate someone switched on.
    permission_override = Column(Boolean)
    allowed_domains = Column(JSONB, nullable=False, server_default="[]")
    timeout_ms = Column(Integer, nullable=False, server_default="10000")
    memory_mb = Column(Integer, nullable=False, server_default="256")
    position = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_skill_functions_skill", "skill_id"),
        Index("uq_skill_functions_name", "skill_id", "name", unique=True),
    )


class SkillExecution(Base):
    """Audit trail for one attempted skill-function execution.

    Deliberately thin: who asked, what was called, whether consent was needed
    and given, and how it ended. Arguments are stored because they are what
    makes a permission decision reviewable; the *result* is not — only its
    size — and skill variables never appear here at all.
    """

    __tablename__ = "skill_executions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    skill_id = Column(
        UUID(as_uuid=True), ForeignKey("usecase_skills.id", ondelete="SET NULL")
    )
    use_case = Column(String(50), nullable=False)
    skill_name = Column(String(100), nullable=False)
    function_name = Column(String(100), nullable=False)
    session_id = Column(UUID(as_uuid=True))
    request_id = Column(UUID(as_uuid=True))
    username = Column(String(100))
    args = Column(JSONB, nullable=False, server_default="[]")
    permission_required = Column(Boolean, nullable=False, server_default="false")
    permission_granted = Column(Boolean)
    status = Column(String(20), nullable=False)
    error = Column(Text)
    result_bytes = Column(Integer, nullable=False, server_default="0")
    duration_ms = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'error', 'denied', 'timeout', 'unresolved')",
            name="ck_skill_executions_status",
        ),
        Index("idx_skill_executions_use_case", "use_case", "created_at"),
        Index("idx_skill_executions_session", "session_id"),
    )


class ApiToken(Base):
    """Bearer credential for the public pipeline API.

    Only the SHA-256 hash is stored; the plaintext is shown once at creation.
    ``use_cases`` is a scope list — empty means "every use case", which mirrors
    how an admin account relates to ``user_use_cases``.
    """

    __tablename__ = "api_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    token_prefix = Column(String(24), nullable=False)
    token_hash = Column(Text, nullable=False, unique=True)
    use_cases = Column(JSONB, nullable=False, server_default="[]")
    created_by = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))


class ApiJob(Base):
    """One asynchronous request against the public pipeline API.

    ``id`` is the request id handed back to the caller and is reused as the
    ``queries.id`` written once the pipeline finishes — so job status, chat
    transcript and feedback all key off the same identifier.
    """

    __tablename__ = "api_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_id = Column(
        UUID(as_uuid=True), ForeignKey("api_tokens.id", ondelete="SET NULL")
    )
    use_case = Column(String(50), nullable=False)
    session_id = Column(UUID(as_uuid=True), nullable=False)
    role = Column(String(20), nullable=False, server_default="default")
    status = Column(String(20), nullable=False, server_default="queued")
    query_text = Column(Text, nullable=False)
    images = Column(JSONB)
    history = Column(JSONB)
    config = Column(JSONB)
    result = Column(JSONB)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error')",
            name="ck_api_jobs_status",
        ),
        Index("idx_api_jobs_queued", "status", "created_at"),
        Index("idx_api_jobs_session", "session_id"),
    )
