CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS keywords (
    id BIGSERIAL PRIMARY KEY,
    term TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    lane TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS source_configs (
    id BIGSERIAL PRIMARY KEY,
    source_name TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    lane TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    alert_threshold_minutes INTEGER NOT NULL DEFAULT 120,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id BIGINT PRIMARY KEY REFERENCES source_configs(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    priority TEXT NOT NULL,
    lane TEXT NOT NULL,
    last_success_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    health_status TEXT NOT NULL DEFAULT 'unknown',
    alert_threshold_minutes INTEGER NOT NULL DEFAULT 120
);

CREATE TABLE IF NOT EXISTS mentions (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    author_or_publisher TEXT,
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    language TEXT,
    country_or_market TEXT,
    raw_text TEXT NOT NULL,
    text_excerpt TEXT,
    matched_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_hash TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    duplicate_group_id TEXT,
    is_new BOOLEAN NOT NULL DEFAULT TRUE,
    is_updated BOOLEAN NOT NULL DEFAULT FALSE,
    backfill BOOLEAN NOT NULL DEFAULT FALSE,
    tos_method TEXT NOT NULL,
    fetch_status TEXT NOT NULL DEFAULT 'ok',
    fetch_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mentions_canonical_url ON mentions(canonical_url);
CREATE INDEX IF NOT EXISTS idx_mentions_event_fingerprint ON mentions(event_fingerprint);
CREATE INDEX IF NOT EXISTS idx_mentions_backfill ON mentions(backfill);
CREATE INDEX IF NOT EXISTS idx_mentions_fetched_at ON mentions(fetched_at);

CREATE TABLE IF NOT EXISTS analysis_results (
    id BIGSERIAL PRIMARY KEY,
    mention_id BIGINT NOT NULL REFERENCES mentions(id) ON DELETE CASCADE,
    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    category TEXT NOT NULL,
    summary_zh TEXT NOT NULL,
    key_quotes JSONB NOT NULL DEFAULT '[]'::jsonb,
    key_quote_offsets JSONB NOT NULL DEFAULT '[]'::jsonb,
    requires_escalation BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_reason TEXT NOT NULL,
    confidence NUMERIC NOT NULL DEFAULT 0,
    evidence_check_passed BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_check_notes TEXT,
    needs_human_review BOOLEAN NOT NULL DEFAULT FALSE,
    analysis_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    review_status TEXT NOT NULL DEFAULT 'unreviewed'
);

CREATE INDEX IF NOT EXISTS idx_analysis_mention_id ON analysis_results(mention_id);
CREATE INDEX IF NOT EXISTS idx_analysis_risk_level ON analysis_results(risk_level);

CREATE TABLE IF NOT EXISTS incident_groups (
    id BIGSERIAL PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    primary_topic TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    risk_level_max TEXT NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    representative_mention_id BIGINT REFERENCES mentions(id),
    status TEXT NOT NULL DEFAULT 'active',
    muted_until TIMESTAMPTZ,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    mention_id BIGINT NOT NULL REFERENCES mentions(id) ON DELETE CASCADE,
    incident_group_id BIGINT REFERENCES incident_groups(id),
    risk_level TEXT NOT NULL,
    alert_reason TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    confidence NUMERIC NOT NULL,
    evidence_check_passed BOOLEAN NOT NULL,
    needs_human_review BOOLEAN NOT NULL,
    delivery_latency_seconds INTEGER,
    sent_to TEXT,
    sent_at TIMESTAMPTZ,
    ack_status TEXT NOT NULL DEFAULT 'pending',
    muted_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_dedupe_key ON alerts(dedupe_key);

CREATE TABLE IF NOT EXISTS daily_reports (
    id BIGSERIAL PRIMARY KEY,
    report_date DATE NOT NULL,
    timezone TEXT NOT NULL,
    total_mentions INTEGER NOT NULL,
    new_mentions INTEGER NOT NULL,
    backfill_mentions INTEGER NOT NULL,
    sentiment_distribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_distribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_distribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    top_risks JSONB NOT NULL DEFAULT '[]'::jsonb,
    trend_vs_yesterday JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommended_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    generated_text_zh TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_reports_date_tz ON daily_reports(report_date, timezone);
