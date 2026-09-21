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
    -- Plugin-Layer: optional GENAU EINE Code-Datei pro Skill (.py oder .js,
    -- nicht gemischt). Alles nullable — Skills ohne Funktionsteil laufen
    -- unveraendert weiter.
    code_language VARCHAR(20)
        CHECK (code_language IS NULL OR code_language IN ('python', 'javascript')),
    code_file_name VARCHAR(255),
    code TEXT,
    -- Skill-Variablen: dynamische Name/Wert-Paare, frei pro Skill definierbar.
    -- NICHT ueber die normale Skill-Liste ausgeliefert — die Werte verlassen
    -- die DB nur im Ausfuehrungsschritt und ueber den Admin-Endpoint.
    variables JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (use_case, name)
);
CREATE INDEX idx_usecase_skills_use_case ON usecase_skills(use_case);

-- ── Plugin-Layer: ausfuehrbare Skill-Funktionen ───────────────────────────
-- Eine Zeile pro aufrufbarer Funktion, beim Speichern der Code-Datei aus dem
-- Quelltext abgeleitet. Das Permission-Flag wird hier EINMAL festgeschrieben:
-- die Ausfuehrung liest es aus dieser Zeile, nie erneut aus dem Quelltext.
CREATE TABLE usecase_skill_functions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id UUID NOT NULL REFERENCES usecase_skills(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    params JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Was die Code-Datei deklariert (aus '# ask for permission' abgeleitet).
    ask_for_permission BOOLEAN NOT NULL DEFAULT FALSE,
    -- Was ein Admin im Dashboard entschieden hat. NULL = der Datei folgen.
    -- Getrennt gehalten, weil das Speichern der Code-Datei die Funktionszeilen
    -- neu schreibt: mit nur einer Spalte ginge eine im UI eingeschaltete
    -- Rueckfrage beim naechsten Speichern still verloren.
    permission_override BOOLEAN,
    -- Leere Liste = kein Netzzugriff (Container laeuft mit --network none).
    allowed_domains JSONB NOT NULL DEFAULT '[]'::jsonb,
    timeout_ms INTEGER NOT NULL DEFAULT 10000,
    memory_mb INTEGER NOT NULL DEFAULT 256,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (skill_id, name)
);
CREATE INDEX idx_skill_functions_skill ON usecase_skill_functions(skill_id);

-- Minimale Nachvollziehbarkeit (Abschnitt 6): wer/was/wann, Ergebnis nur als
-- Groesse und Status — keine Nutzdaten, keine Variablenwerte.
CREATE TABLE skill_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id UUID REFERENCES usecase_skills(id) ON DELETE SET NULL,
    use_case VARCHAR(50) NOT NULL,
    skill_name VARCHAR(100) NOT NULL,
    function_name VARCHAR(100) NOT NULL,
    session_id UUID,
    request_id UUID,
    username VARCHAR(100),
    args JSONB NOT NULL DEFAULT '[]'::jsonb,
    permission_required BOOLEAN NOT NULL DEFAULT FALSE,
    permission_granted BOOLEAN,
    status VARCHAR(20) NOT NULL
        CHECK (status IN ('ok', 'error', 'denied', 'timeout', 'unresolved')),
    error TEXT,
    result_bytes INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX idx_skill_executions_use_case ON skill_executions(use_case, created_at);
CREATE INDEX idx_skill_executions_session ON skill_executions(session_id);

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

-- ══════════════════════════════════════════════════════════════
-- Öffentliche Pipeline-API (Bearer-Token + asynchrone Jobs)
-- ══════════════════════════════════════════════════════════════

-- Bearer-Tokens für die externe API. Gespeichert wird nur der SHA-256-Hash;
-- der Klartext ist genau einmal (beim Anlegen) sichtbar. `use_cases` schränkt
-- ein Token auf bestimmte Use Cases ein — leere Liste = alle.
CREATE TABLE api_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    token_prefix VARCHAR(24) NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    use_cases JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE
);

-- Eine Zeile pro API-Anfrage. Bewusst dauerhaft: der Aufrufer pollt mit der
-- Request-ID, der Zustand muss also einen Neustart überleben. Die `id` ist
-- zugleich die Request-ID und die spätere `queries.id` — Job, Transkript und
-- Feedback teilen sich damit einen Identifier.
CREATE TABLE api_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id UUID REFERENCES api_tokens(id) ON DELETE SET NULL,
    use_case VARCHAR(50) NOT NULL,
    session_id UUID NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'default',
    status VARCHAR(20) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'error')),
    query_text TEXT NOT NULL,
    images JSONB,
    history JSONB,
    config JSONB,
    result JSONB,
    error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX idx_api_jobs_queued ON api_jobs(status, created_at);
CREATE INDEX idx_api_jobs_session ON api_jobs(session_id);

-- Indizes
CREATE INDEX idx_queries_use_case ON queries(use_case);
CREATE INDEX idx_queries_created_at ON queries(created_at);
CREATE INDEX idx_ingestion_file_hash ON ingestion_log(file_hash);
CREATE INDEX idx_feedback_query_id ON feedback(query_id);
