-- SIH26122: Add actual tracking columns to schedule_tasks table.
-- Automatically updated from AI-processed site updates.
-- Baseline planned_start and planned_end dates are strictly preserved.

ALTER TABLE schedule_tasks
    ADD COLUMN IF NOT EXISTS actual_start_date DATE,
    ADD COLUMN IF NOT EXISTS actual_end_date DATE,
    ADD COLUMN IF NOT EXISTS progress_percent NUMERIC(5, 2),
    ADD COLUMN IF NOT EXISTS status TEXT,
    ADD COLUMN IF NOT EXISTS delay_days INTEGER,
    ADD COLUMN IF NOT EXISTS delay_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'schedule_tasks_progress_percent_check'
    ) THEN
        ALTER TABLE schedule_tasks
            ADD CONSTRAINT schedule_tasks_progress_percent_check
                CHECK (progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'schedule_tasks_status_check'
    ) THEN
        ALTER TABLE schedule_tasks
            ADD CONSTRAINT schedule_tasks_status_check
                CHECK (
                    status IS NULL OR status IN (
                        'not_started',
                        'in_progress',
                        'completed',
                        'delayed',
                        'blocked'
                    )
                );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'schedule_tasks_delay_days_check'
    ) THEN
        ALTER TABLE schedule_tasks
            ADD CONSTRAINT schedule_tasks_delay_days_check
                CHECK (delay_days IS NULL OR delay_days >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'schedule_tasks_actual_dates_check'
    ) THEN
        ALTER TABLE schedule_tasks
            ADD CONSTRAINT schedule_tasks_actual_dates_check
                CHECK (
                    actual_start_date IS NULL
                    OR actual_end_date IS NULL
                    OR actual_end_date >= actual_start_date
                );
    END IF;
END $$;
