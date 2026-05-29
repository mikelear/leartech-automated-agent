-- 0001_initial.sql — DB-backed initiative catalog schema.
--
-- One table: initiative_catalog. Stores raw YAML so the schema is
-- evolution-free — the Python loader parses on read using the same
-- pydantic model as the filesystem path. See app/db/models.py.
--
-- IF NOT EXISTS makes this safely idempotent — the deployment's
-- migrations initContainer runs on every pod start, re-applying this
-- file each time.

CREATE TABLE IF NOT EXISTS initiative_catalog (
    name         VARCHAR(255) PRIMARY KEY,
    yaml_body    TEXT         NOT NULL,
    description  TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by   VARCHAR(255)  -- NULL until auth integration lands
);

-- updated_at trigger: bump on every UPDATE so the column reflects the
-- last mutation time, not the create time. (Postgres doesn't auto-update
-- the column like MySQL does.)
CREATE OR REPLACE FUNCTION initiative_catalog_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS initiative_catalog_updated_at ON initiative_catalog;
CREATE TRIGGER initiative_catalog_updated_at
    BEFORE UPDATE ON initiative_catalog
    FOR EACH ROW
    EXECUTE FUNCTION initiative_catalog_set_updated_at();
