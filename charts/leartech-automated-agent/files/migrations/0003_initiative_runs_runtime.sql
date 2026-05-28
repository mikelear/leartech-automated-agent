-- 0003_initiative_runs_runtime.sql — Phase D.4 dual-path runtime fields.
--
-- Adds two columns to initiative_runs:
--   * `runtime`  ('asyncio' | 'job') — which spawn path created this run.
--     Default 'asyncio' so older rows that pre-date D.4 round-trip cleanly.
--   * `job_name` (nullable VARCHAR(64)) — the K8s Job name when
--     runtime='job'. NULL on the asyncio path. Per D.3 contract job_name
--     equals run_id, so the index here is unique except for the NULLs.
--
-- ADD COLUMN IF NOT EXISTS makes this safely idempotent — the migrations
-- Helm hook runs on every install + upgrade.
--
-- See app/db/models.py::InitiativeRunRow for the SQLAlchemy companion.

ALTER TABLE initiative_runs
    ADD COLUMN IF NOT EXISTS runtime VARCHAR(16) NOT NULL DEFAULT 'asyncio';

ALTER TABLE initiative_runs
    ADD COLUMN IF NOT EXISTS job_name VARCHAR(64);

-- Index by runtime so the future status-reconciler (D.5) can list
-- Job-runtime rows without a full table scan.
CREATE INDEX IF NOT EXISTS idx_runs_runtime ON initiative_runs(runtime);
