-- 0009_tenant_id.sql — multi-tenant row-level isolation (v7-P1 step 5).
--
-- Mirror of leartech-orchestrator step 4 against this repo's tables.
-- Adds a nullable ``tenant_id`` column to:
--
--   - ``initiative_catalog`` (initiative DEFINITIONS — tenant-scoped libraries)
--   - ``initiative_runs``    (EXECUTIONS — i.e. agent_runs in the v7-P1 spec)
--
-- Together with the auth middleware shipped in step 2 (which extracts
-- ``tenant_id`` from the bearer's ``tenant_id`` claim and attaches it to
-- ``request.state``), this is the foundation for multi-tenant data
-- isolation: writers persist the caller's tenant_id; readers filter by
-- tenant_id; cross-tenant lookups 404 (not 403 — leaking presence is the
-- same harm as leaking content).
--
-- Why NULLable, not NOT NULL:
--
--   - Backfill safety: existing rows pre-date the auth middleware and
--     have no tenant context. Backfilling to a sentinel like 'system'
--     conflates "global, system-owned" with "legacy, unknown".
--   - Global catalog entries: NULL tenant_id is the encoding for "global
--     initiative visible to every tenant" (the system tenant's library).
--     The catalog reader returns ``WHERE tenant_id IS NULL OR tenant_id = ?``
--     so each tenant sees their own library + the global set.
--
-- agent_run_decisions and agent_run_snapshots (0006) intentionally do
-- NOT get a tenant_id column — they're CASCADE-deleted with their
-- parent ``initiative_runs`` row, so tenant scoping flows through the
-- FK. Denormalising tenant_id there would only buy us standalone-table
-- queries that don't exist today; can be added later if a use case
-- emerges.
--
-- Reversibility: ADD COLUMN IF NOT EXISTS is the agreed pattern; the
-- inverse is ``ALTER TABLE ... DROP COLUMN tenant_id``. Both are
-- non-destructive for non-tenant traffic (which writes NULL today).
--
-- IF NOT EXISTS makes this safely idempotent — the deployment's
-- migrations initContainer runs on every pod start, re-applying this
-- file each time.
--
-- See app/db/models.py for the SQLAlchemy companions and
-- app/db/initiative_catalog.py + app/db/initiative_runs.py for the
-- writer/reader implementations.

ALTER TABLE initiative_catalog
    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(255);

ALTER TABLE initiative_runs
    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(255);

-- Tenant-scoped read paths filter by tenant_id; index it so the filter
-- is a btree seek rather than a sequential scan once the tables grow.
CREATE INDEX IF NOT EXISTS idx_initiative_catalog_tenant_id ON initiative_catalog(tenant_id);
CREATE INDEX IF NOT EXISTS idx_initiative_runs_tenant_id    ON initiative_runs(tenant_id);
