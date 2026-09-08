-- SIH26122: Add last_reported_on column to schedule_tasks table.
-- Protects newer/higher-progress actual state from being overwritten by older/lower-progress site updates.

ALTER TABLE schedule_tasks
    ADD COLUMN IF NOT EXISTS last_reported_on DATE;

-- Backfill last_reported_on from current ai_processed_updates joined with site_updates
UPDATE schedule_tasks st
SET last_reported_on = sub.reported_on
FROM (
    SELECT DISTINCT ON (ap.matched_task_id)
        ap.matched_task_id,
        su.reported_on
    FROM ai_processed_updates ap
    JOIN site_updates su ON ap.site_update_id = su.id
    WHERE ap.matched_task_id IS NOT NULL
      AND ap.is_current = true
    ORDER BY ap.matched_task_id, su.reported_on DESC
) sub
WHERE st.id = sub.matched_task_id;
