-- SIH26122: Add archived_at column and index to site_updates table.
-- Enables data isolation for replaced schedule baselines while preserving historical records for audit.

ALTER TABLE site_updates
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS site_updates_archived_at_idx ON site_updates (archived_at);

