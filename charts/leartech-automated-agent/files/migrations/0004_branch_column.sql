-- 0004_branch_column.sql — Phase D.5.1.2 persists the initiative-declared
-- branch name on each run row.
--
-- D.5.1.1 (#74) added a GH-side PR lookup fallback in the reconciler, but
-- inferred the branch from `agent/<record.initiative>`. That convention
-- doesn't match real initiative branches (e.g. the goal name is
-- `f-default-job-drop-asyncio` but the YAML's `branch:` field is
-- `agent/f-default-job-drop-asyncio` — the prefix gets doubled), so the
-- fallback always missed and `pr_number` stayed None.
--
-- Persisting the YAML-declared branch on the DB row eliminates the
-- guesswork: the reconciler just reads `record.branch` and queries
-- `gh pr list --head <branch>`. Authoritative + no catalog round-trip.
--
-- Old rows pre-migration get NULL; the reconciler treats NULL as "no
-- fallback available" and falls through to log-parse only.
--
-- ADD COLUMN IF NOT EXISTS makes this safely idempotent — the deployment's
-- migrations initContainer runs on every pod start, re-applying this file
-- each time. CREATE INDEX IF NOT EXISTS likewise.
--
-- See app/db/models.py::InitiativeRunRow for the SQLAlchemy companion.

ALTER TABLE initiative_runs
    ADD COLUMN IF NOT EXISTS branch VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_runs_branch ON initiative_runs(branch);
