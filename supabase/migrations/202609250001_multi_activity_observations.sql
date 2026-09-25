-- Phase 2 Multi-Activity Observations Migration

-- 1. Drop Phase 1 single-current-row constraint
DROP INDEX IF EXISTS ai_processed_updates_current_site_update_unique_idx;

-- 2. Add observation_index (0 = primary, 1+ = secondary)
ALTER TABLE ai_processed_updates
    ADD COLUMN IF NOT EXISTS observation_index INTEGER NOT NULL DEFAULT 0;

-- 3. New idempotency constraint: at most one current row per (update, index)
CREATE UNIQUE INDEX IF NOT EXISTS ai_processed_updates_current_obs_unique_idx
    ON ai_processed_updates (site_update_id, observation_index)
    WHERE is_current = true;

-- 4. Retrieval index for fetching current and historical observations by site update
CREATE INDEX IF NOT EXISTS ai_processed_updates_site_update_obs_idx
    ON ai_processed_updates (site_update_id, observation_index);
