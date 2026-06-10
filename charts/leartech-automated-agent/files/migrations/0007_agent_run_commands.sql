-- 0007_agent_run_commands.sql — bidirectional command queue between
-- the operator and a running agent (initiative
-- agent-add-command-queue-with-injection).
--
-- Today, once an initiative agent starts running, an operator can only
-- watch — there is no way to cancel gracefully (with a custom reason
-- that surfaces in failure diagnostics), pause/resume, or inject
-- guidance ("stop using ghcr — use docker.io").
--
-- This migration adds a single ``agent_run_commands`` table that the
-- SDK loop polls between turns. Commands queue up here; the agent
-- drains them at each turn boundary, applies their semantics, and
-- writes an ack row back so the operator can confirm delivery.
--
-- Why a DB table and not Redis / NATS / a tmpfs:
--   - The runtime already has Postgres (we use it for the
--     initiative_runs row, the catalog, decisions log, snapshot).
--     Adding a queue infrastructure would multiply moving parts.
--   - Polling latency = one turn boundary (~5-15s) is acceptable for
--     v1. A turn rarely runs longer than 30s. The operator already
--     waits orders of magnitude longer for the SDK to surface state.
--   - Ack is durable across pod restarts. If the agent dies before
--     processing a command, the unacked row survives and the new pod
--     (in the V6 resumption shape) picks it up.
--
-- Command types (CHECK-enforced at the DB level so a typo from the CLI
-- can't smuggle through):
--   cancel           — graceful shutdown; reason in payload.reason
--   pause            — agent waits for matching resume command
--   resume           — release a paused agent
--   inject_guidance  — payload.text appended as a UserMessage to the
--                      conversation, surfaced to the model on the next
--                      turn
--
-- Indexing strategy:
--   ix_agent_run_commands_unacked is a PARTIAL index — covers only
--   rows where acked_at IS NULL. The poll path's hot query is
--     SELECT * FROM agent_run_commands
--     WHERE run_id = ? AND acked_at IS NULL
--     ORDER BY submitted_at;
--   Partial index keeps the working set tiny (acked rows fall out)
--   so the per-turn poll is sub-millisecond even at high command
--   volumes.
--
-- IF NOT EXISTS keeps the migration idempotent — the initContainer
-- reapplies it on every pod start.

CREATE TABLE IF NOT EXISTS agent_run_commands (
    id            BIGSERIAL    PRIMARY KEY,
    run_id        VARCHAR(64)  NOT NULL REFERENCES initiative_runs(id) ON DELETE CASCADE,
    command_type  VARCHAR(32)  NOT NULL CHECK (command_type IN
        ('cancel', 'pause', 'resume', 'inject_guidance')),
    payload       JSONB,
    submitted_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    acked_at      TIMESTAMPTZ,
    ack_message   TEXT
);

CREATE INDEX IF NOT EXISTS ix_agent_run_commands_unacked
    ON agent_run_commands(run_id, submitted_at)
    WHERE acked_at IS NULL;
