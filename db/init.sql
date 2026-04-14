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

-- Indizes
CREATE INDEX idx_queries_use_case ON queries(use_case);
CREATE INDEX idx_queries_created_at ON queries(created_at);
CREATE INDEX idx_ingestion_file_hash ON ingestion_log(file_hash);
CREATE INDEX idx_feedback_query_id ON feedback(query_id);
