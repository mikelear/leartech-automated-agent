-- 0006_agent_run_diagnostics.sql — comprehensive failure-diagnostic
-- capture (initiative agent-add-comprehensive-failure-diagnostics).
--
-- Adds two companion tables to ``initiative_runs``:
--
--   ``agent_run_decisions``   — one row per notable agent decision
--                                inflection point (tool call, gate
--                                classification, retry, terminate).
--                                Lets operators reconstruct WHAT the
--                                agent did turn-by-turn without
--                                pod-log archaeology.
--
--   ``agent_run_snapshots``   — one row per terminal run carrying the
--                                full SDK conversation history as
--                                JSONB. Lets operators see the verbatim
--                                LLM turns (system prompt, user prompt,
--                                every assistant + tool message) for
--                                forensics.
--
-- Together with Layer 1 (``initiative_runs.error``, populated by the
-- run-driver's failure-classifier) and Layer 4 (SIGTERM/atexit handler
-- flushing both tables before exit), these give operators a complete
-- failure-postmortem from the DB alone — never blind again.
--
-- Why JSONB for snapshots (Option A from the initiative spec):
--   - Postgres ``jsonb`` TOASTs values >2KB automatically — a 100KB
--     conversation gets compressed + out-of-line stored transparently.
--     No need for GCS plumbing today.
--   - Operator queries (``SELECT messages FROM agent_run_snapshots
--     WHERE run_id = 'X'``) work in psql with no extra tooling.
--   - Future migration to object storage is straightforward: add a
--     ``storage_uri`` column, dual-write, swap reads.
--
-- Both tables are write-mostly (one INSERT per decision, one INSERT
-- per run terminal). Reads are ad-hoc operator queries by ``run_id``.
-- An index on ``run_id`` covers the dominant access pattern; rows are
-- also queryable by ``created_at`` for time-range diagnostics.
--
-- Cascade: ON DELETE CASCADE ties the lifetime of diagnostics rows to
-- the parent run row. If an operator deletes a run record, its
-- diagnostics go with it — no orphan rows.
--
-- IF NOT EXISTS / IF EXISTS makes the migration idempotent — the
-- deployment's initContainer re-applies it on every pod start.

CREATE TABLE IF NOT EXISTS agent_run_decisions (
    id          BIGSERIAL    PRIMARY KEY,
    run_id      VARCHAR(64)  NOT NULL REFERENCES initiative_runs(id) ON DELETE CASCADE,
    turn_index  INTEGER      NOT NULL,
    kind        VARCHAR(32)  NOT NULL,            -- 'tool_call' | 'decision' | 'wait' | 'gate' | 'terminate' | 'sigterm' | ...
    summary     TEXT         NOT NULL,            -- one-paragraph human-readable
    payload     JSONB,                            -- tool inputs/outputs or NULL
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_agent_run_decisions_run_id      ON agent_run_decisions(run_id);
CREATE INDEX IF NOT EXISTS ix_agent_run_decisions_created_at  ON agent_run_decisions(created_at DESC);

-- One snapshot per run — UNIQUE on run_id. The SIGTERM handler may fire
-- AFTER the natural terminal write; the second INSERT becomes an UPSERT
-- (handled at the CRUD layer via ON CONFLICT DO UPDATE) so we never
-- have two snapshot rows racing for the same run.
CREATE TABLE IF NOT EXISTS agent_run_snapshots (
    run_id           VARCHAR(64)  PRIMARY KEY REFERENCES initiative_runs(id) ON DELETE CASCADE,
    messages         JSONB        NOT NULL,
    message_count    INTEGER      NOT NULL,       -- denormalised so SELECT count(*) doesn't need to parse JSONB
    terminal_reason  VARCHAR(64),                 -- 'complete' | 'failed' | 'sigterm' | 'max_turns' | ...
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_agent_run_snapshots_created_at  ON agent_run_snapshots(created_at DESC);
