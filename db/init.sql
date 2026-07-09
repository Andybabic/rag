-- ══════════════════════════════════════════════════════════════
-- RAG Platform – Initial Database Schema
-- ══════════════════════════════════════════════════════════════

-- Alle Anfragen und Antworten
CREATE TABLE queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    use_case VARCHAR(50) NOT NULL,
    session_id UUID,
    role VARCHAR(20) DEFAULT 'default',
    query_text TEXT NOT NULL,
    answer_text TEXT,
    chunks_used JSONB,
    agent_steps JSONB,
    citations JSONB,
    images JSONB,
    scores JSONB,
    sufficient BOOLEAN,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Nutzer-Feedback
CREATE TABLE feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id UUID REFERENCES queries(id),
    rating VARCHAR(10) CHECK (rating IN ('positive', 'negative')),
    comment TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Ingestion-Log (Resume-Fähigkeit + Dokumenten-Zugriff)
CREATE TABLE ingestion_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_name VARCHAR(500) NOT NULL,
    file_hash VARCHAR(64) NOT NULL UNIQUE,
    stored_path VARCHAR(1000),
    collection VARCHAR(100),
    use_case VARCHAR(50),
    chunk_count INTEGER,
    status VARCHAR(20) DEFAULT 'ok',
    error_detail TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Agent-Gedächtnis (ersetzt memory.md aus bestehender Pipeline)
CREATE TABLE agent_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    use_case VARCHAR(50) NOT NULL UNIQUE,
    memory_text TEXT,
    version INTEGER DEFAULT 1,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Reranking-Ergebnisse für späteres Fine-Tuning
CREATE TABLE evaluation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id UUID REFERENCES queries(id),
    reranked_chunks JSONB,
    threshold_passed BOOLEAN,
    refined_queries JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Per-Use-Case Konfiguration (Provider, Modelle, API-Keys verschluesselt).
-- NULL bedeutet "Fallback auf .env".
CREATE TABLE usecase_config (
    use_case VARCHAR(50) PRIMARY KEY,
    chat_provider VARCHAR(20),
    embedding_provider VARCHAR(20),
    vision_provider VARCHAR(20),
    ollama_base_url TEXT,
    ollama_api_key_encrypted TEXT,
    openai_base_url TEXT,
    openai_api_key_encrypted TEXT,
    llm_model VARCHAR(100),
    embedding_model VARCHAR(100),
    vision_model VARCHAR(100),
    embedding_dimension INTEGER,
    temperature REAL,
    max_tokens INTEGER,
    embed_batch_size INTEGER,
    agent_max_steps INTEGER,
    memory_max_chars INTEGER,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Editierbare Prompts pro Use Case. use_case='*' fuer globale Prompts.
CREATE TABLE usecase_prompts (
    use_case VARCHAR(50) NOT NULL,
    prompt_key VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (use_case, prompt_key)
);

-- Skills / Regeln, die an den Agent-System-Prompt angehaengt werden.
CREATE TABLE usecase_skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    use_case VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    overview TEXT NOT NULL,
    detailed_task TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (use_case, name)
);
CREATE INDEX idx_usecase_skills_use_case ON usecase_skills(use_case);

-- DB-gestuetzte Use-Case-Registry. Wird aus config/use_cases.json geseedet
-- und danach ueber das Dashboard editierbar. 'id' = API-Id (use_case-Key
-- ueberall), 'slug' = URL-Form im Frontend. Prompts liegen in usecase_prompts.
CREATE TABLE use_cases (
    id VARCHAR(50) PRIMARY KEY,
    slug VARCHAR(50) NOT NULL UNIQUE,
    label VARCHAR(100) NOT NULL,
    description TEXT DEFAULT '',
    color VARCHAR(40) DEFAULT '',
    accent VARCHAR(20) DEFAULT '',
    roles JSONB NOT NULL,
    agent_action_names JSONB NOT NULL,
    default_collection VARCHAR(100) NOT NULL,
    collection_prefixes JSONB,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Dashboard-/App-Benutzer. Passwoerter gehasht (PBKDF2-SHA256).
-- Rollen: 'admin' (User + Use Cases verwalten) und 'user' (nur Chat).
-- Der erste Admin wird beim Start aus den Env-Variablen geseedet.
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Tombstones für gelöschte Use Cases. Der Boot-Seed spielt Standard-Use-Cases
-- aus config/use_cases.json ein (create-if-missing); ohne diesen Merker kämen
-- gelöschte Standard-Use-Cases nach jedem Neustart zurück. Der Seed überspringt
-- getombstonete IDs; ein Neuanlegen derselben ID entfernt den Tombstone.
CREATE TABLE deleted_use_cases (
    use_case VARCHAR(50) PRIMARY KEY,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Zuordnung Benutzer ↔ Use Cases (Zugriffssteuerung).
-- Leere Zuordnung = kein Zugriff. Admins haben immer Zugriff auf alles
-- (in der Frontend-Schicht durchgesetzt, nicht im Schema).
CREATE TABLE user_use_cases (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    use_case VARCHAR(50) NOT NULL REFERENCES use_cases(id) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (user_id, use_case)
);
CREATE INDEX idx_user_use_cases_user ON user_use_cases(user_id);

-- Indizes
CREATE INDEX idx_queries_use_case ON queries(use_case);
CREATE INDEX idx_queries_created_at ON queries(created_at);
CREATE INDEX idx_ingestion_file_hash ON ingestion_log(file_hash);
CREATE INDEX idx_feedback_query_id ON feedback(query_id);
