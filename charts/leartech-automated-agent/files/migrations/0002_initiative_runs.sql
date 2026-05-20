-- 0002_initiative_runs.sql — durable run-state store.
--
-- One row per initiative execution. Tracks status, timing, and outcome so
-- run history survives pod restarts. The `initiative_catalog` table (0001)
-- stores initiative DEFINITIONS; this table stores EXECUTIONS.
--
-- IF NOT EXISTS makes this safely idempotent — the migrations Helm hook
-- runs on every install and upgrade.

CREATE TABLE IF NOT EXISTS initiative_runs (
    id            VARCHAR(64)   PRIMARY KEY,
    initiative    VARCHAR(255)  NOT NULL,
    status        VARCHAR(32)   NOT NULL,        -- queued | running | complete | failed | cancelled | orphaned | timed_out
    started_at    TIMESTAMPTZ   NOT NULL,
    finished_at   TIMESTAMPTZ,
    pr_number     INTEGER,
    pr_repo       VARCHAR(255),
    turns         INTEGER,
    cost_usd      NUMERIC(10, 4),
    error         TEXT,
    cluster       VARCHAR(32),
    created_by    VARCHAR(255),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runs_status      ON initiative_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_initiative  ON initiative_runs(initiative);
CREATE INDEX IF NOT EXISTS idx_runs_started_at  ON initiative_runs(started_at DESC);

-- updated_at trigger: bump on every UPDATE so the column reflects the
-- last mutation time, not the create time. (Postgres doesn't auto-update
-- the column like MySQL does.)
CREATE OR REPLACE FUNCTION initiative_runs_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS initiative_runs_updated_at ON initiative_runs;
CREATE TRIGGER initiative_runs_updated_at
    BEFORE UPDATE ON initiative_runs
    FOR EACH ROW
    EXECUTE FUNCTION initiative_runs_set_updated_at();
