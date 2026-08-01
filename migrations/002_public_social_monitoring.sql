ALTER TABLE mentions ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'web';
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS content_type TEXT NOT NULL DEFAULT 'article';
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS provider_item_id TEXT NOT NULL DEFAULT '';
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS parent_url TEXT NOT NULL DEFAULT '';
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS discovery_method TEXT NOT NULL DEFAULT 'public_feed';
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS coverage_tier TEXT NOT NULL DEFAULT 'public_index';
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS provider_added_at TIMESTAMPTZ;
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS discovery_latency_seconds INTEGER;
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS view_count BIGINT;
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS like_count BIGINT;
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS comment_count BIGINT;
ALTER TABLE mentions ADD COLUMN IF NOT EXISTS share_count BIGINT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mentions_provider_item
ON mentions(provider, provider_item_id)
WHERE provider <> '' AND provider_item_id <> '';

ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS campaign TEXT NOT NULL DEFAULT 'brand_major_risk';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS relevance NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS novelty_type TEXT NOT NULL DEFAULT 'new_mention';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS notification_priority TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS recommended_action TEXT NOT NULL DEFAULT 'monitor';

CREATE TABLE IF NOT EXISTS mention_metrics (
    id BIGSERIAL PRIMARY KEY,
    mention_id BIGINT NOT NULL REFERENCES mentions(id) ON DELETE CASCADE,
    view_count BIGINT,
    like_count BIGINT,
    comment_count BIGINT,
    share_count BIGINT,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mention_metrics_mention_time
ON mention_metrics(mention_id, captured_at);

CREATE TABLE IF NOT EXISTS mention_actions (
    mention_id BIGINT PRIMARY KEY REFERENCES mentions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT '待判断',
    response_url TEXT NOT NULL DEFAULT '',
    internal_note TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    intervention_reason TEXT NOT NULL DEFAULT '',
    draft_original TEXT NOT NULL DEFAULT '',
    draft_zh TEXT NOT NULL DEFAULT '',
    legal_risk_note TEXT NOT NULL DEFAULT '',
    acted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mention_notifications (
    mention_id BIGINT PRIMARY KEY REFERENCES mentions(id) ON DELETE CASCADE,
    notification_priority TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    outbox_dedupe_key TEXT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
