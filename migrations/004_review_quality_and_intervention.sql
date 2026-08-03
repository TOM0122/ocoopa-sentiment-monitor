-- An explicit false-positive review remains an audit record, but it is
-- excluded by application reporting queries. This column records a separate
-- human routing decision and never grants any platform publishing permission.
ALTER TABLE mention_actions
ADD COLUMN IF NOT EXISTS intervention_status TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_incident_groups_status_last_seen
ON incident_groups(status, last_seen_at DESC);
