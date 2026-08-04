-- Distinguish a successful HTTP/API call from an actual discovery outcome.
-- These fields contain counts only, with no query credentials or platform content
-- are stored in source health.
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_result_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_matched_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_stored_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_filtered_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_duplicate_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_health ADD COLUMN IF NOT EXISTS last_query_label TEXT NOT NULL DEFAULT '';
