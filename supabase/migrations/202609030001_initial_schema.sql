-- SIH26122 initial Supabase/PostgreSQL schema.
-- This migration defines the schedule, site-update, and AI-processing foundation.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE schedule_imports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_filename TEXT NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    notes TEXT
);

CREATE TABLE schedule_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_import_id UUID NOT NULL REFERENCES schedule_imports(id),
    source_task_id TEXT NOT NULL,
    activity TEXT NOT NULL,
    location TEXT NOT NULL,
    planned_start DATE NOT NULL,
    planned_end DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT schedule_tasks_planned_dates_check
        CHECK (planned_end >= planned_start),
    CONSTRAINT schedule_tasks_import_source_task_unique
        UNIQUE (schedule_import_id, source_task_id)
);

CREATE TABLE task_dependencies (
    task_id UUID NOT NULL REFERENCES schedule_tasks(id) ON DELETE CASCADE,
    depends_on_task_id UUID NOT NULL REFERENCES schedule_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CONSTRAINT task_dependencies_no_self_reference_check
        CHECK (task_id <> depends_on_task_id)
);

CREATE TABLE site_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_update_id TEXT,
    reported_on DATE NOT NULL,
    location TEXT NOT NULL,
    raw_update TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_reference TEXT
);

CREATE TABLE ai_processed_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_update_id UUID NOT NULL REFERENCES site_updates(id) ON DELETE CASCADE,
    matched_task_id UUID REFERENCES schedule_tasks(id) ON DELETE SET NULL,
    progress_percent NUMERIC(5, 2),
    status TEXT,
    delay_days INTEGER,
    delay_reason TEXT,
    actual_start_date DATE,
    actual_end_date DATE,
    confidence_score NUMERIC(5, 2),
    model_name TEXT,
    model_response JSONB,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_current BOOLEAN NOT NULL DEFAULT true,
    CONSTRAINT ai_processed_updates_progress_percent_check
        CHECK (progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100),
    CONSTRAINT ai_processed_updates_status_check
        CHECK (
            status IS NULL OR status IN (
                'not_started',
                'in_progress',
                'completed',
                'delayed',
                'blocked'
            )
        ),
    CONSTRAINT ai_processed_updates_delay_days_check
        CHECK (delay_days IS NULL OR delay_days >= 0),
    CONSTRAINT ai_processed_updates_actual_dates_check
        CHECK (
            actual_start_date IS NULL
            OR actual_end_date IS NULL
            OR actual_end_date >= actual_start_date
        ),
    CONSTRAINT ai_processed_updates_confidence_score_check
        CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0 AND 100)
);

CREATE INDEX schedule_tasks_schedule_import_id_idx
    ON schedule_tasks (schedule_import_id);

CREATE INDEX task_dependencies_depends_on_task_id_idx
    ON task_dependencies (depends_on_task_id);

CREATE INDEX site_updates_reported_on_idx
    ON site_updates (reported_on);

CREATE INDEX site_updates_location_idx
    ON site_updates (location);

CREATE INDEX ai_processed_updates_matched_task_processed_at_idx
    ON ai_processed_updates (matched_task_id, processed_at DESC);

CREATE UNIQUE INDEX ai_processed_updates_current_site_update_unique_idx
    ON ai_processed_updates (site_update_id)
    WHERE is_current = true;
