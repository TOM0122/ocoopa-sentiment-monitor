-- Reporting windows and daily-new trends use immutable first discovery time.
-- fetched_at remains the latest retrieval timestamp and changes on re-polls.
CREATE INDEX IF NOT EXISTS idx_mentions_first_seen_at
ON mentions(first_seen_at);
