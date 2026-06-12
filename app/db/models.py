"""SQLAlchemy 2.0 models — DB-backed initiative catalogue and run store.

Two tables:
- `initiative_catalog` — initiative DEFINITIONS (raw YAML). Shipped in PR #21.
- `initiative_runs`    — initiative EXECUTIONS (one row per run + outcome).

Stores raw YAML in the catalog so the schema is evolution-free. The runs
table has typed columns — querying by status/initiative/date is a common
operational need.

If future endpoints need filtering by repo/branch/etc. in the catalog,
parse the YAML in the application layer or add a generated column. Don't
normalise.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator


class Base(DeclarativeBase):
    pass


class JSONBOrJSON(TypeDecorator[Any]):
    """JSONB on Postgres, JSON elsewhere (SQLite tests).

    Mirrors the leartech-auth-service pattern: production uses Postgres
    JSONB (TOAST + GIN index support); tests use SQLite which has no
    native JSONB but its JSON1 extension is fine for round-tripping
    Python dicts. Keeping a single column type lets the ORM model
    declaration match both backends without dialect-conditional logic
    in every call site.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class InitiativeRow(Base):
    """One row per DB-stored initiative.

    `name` is the primary key — same identity as a YAML filename stem,
    keeping filesystem and DB initiatives interchangeable from the
    agent's POV. The loader merges them; filesystem wins on conflict.

    `yaml_body` is the full initiative YAML as a string. Stored raw so:
    - Schema doesn't need to change when initiative fields evolve
    - The same pydantic loader handles DB and FS sources
    - History/audit can be reconstructed by diffing rows or via app logic
    """

    __tablename__ = 'initiative_catalog'

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    yaml_body: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        # Placeholder for when auth integration lands — for now NULL means
        # "submitted by unauthenticated caller" (today's mode).
    )

    def __repr__(self) -> str:
        return f'InitiativeRow(name={self.name!r}, updated_at={self.updated_at.isoformat()})'


class InitiativeRunRow(Base):
    """One row per initiative execution.

    Tracks status, timing, and outcome so run history survives pod restarts.
    `initiative_catalog` stores DEFINITIONS; this stores EXECUTIONS.

    `id` is a 12-char hex UUID (matches `app.state.new_id()` — the same ID
    flows through the REST API, asyncio.Task, and DB row).

    Status lifecycle:
      queued → running → complete | failed | cancelled
      queued | running → orphaned  (pod died; reconcile on next startup)
      running → timed_out          (future: max_runtime exceeded)
    """

    __tablename__ = 'initiative_runs'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    initiative: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_repo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cluster: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Phase F: runtime is always 'job' on new rows — the in-process
    # asyncio path was removed. Legacy DB rows from before Phase F may
    # still carry 'asyncio'; the server_default + Python default reflect
    # the new contract for any rows inserted from here on. The column is
    # set at INSERT time by the router and never mutated afterwards.
    runtime: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default='job',
        server_default='job',
    )
    # K8s Job name — equals the run_id by D.3 contract. NULL only on
    # legacy asyncio rows from before Phase F so older rows continue to
    # round-trip cleanly.
    job_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Phase D.5.1.2 — initiative YAML's declared `branch` field. Persisting
    # the authoritative branch (rather than rederiving from `initiative`)
    # lets the job_reconciler's GH-side PR fallback look up the PR by
    # `gh pr list --head <branch>` without name-mangling. NULL on old rows
    # pre-migration; the reconciler treats NULL as "skip fallback".
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # V5 D2.2 — wall-clock time of the FIRST SDK turn the agent
    # actually executed. NULL until that first turn fires; set
    # exactly once thereafter (idempotent — concurrent retries
    # guarded by `started_executing_at IS NULL` in the UPDATE
    # WHERE clause; see gate/agent/run_driver.py::mark_first_turn).
    #
    # Distinct from `started_at` (row-creation time). The V4 stall
    # demonstrated that `turns == 0` alone is ambiguous: a row with
    # turns=0 could be in-flight (first turn fired but not yet
    # complete) OR genuinely stuck (no first turn ever fired). Having
    # `started_executing_at` lets the reconciler distinguish the
    # two — NULL alongside age > threshold is the orphan-eligible
    # shape; NOT NULL means the agent has begun executing.
    #
    # Downstream consumers (V1 launch-readiness, V3 reconciler
    # staleness, V4 image-pull watchdog) PREFER this column over
    # `turns == 0`.
    started_executing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Per-turn writeback surface (initiative
    # agent-add-per-turn-writeback). NAME of the LAST tool the agent
    # invoked during the just-completed turn — operators reading the
    # row see "what is the agent doing right now?" without parsing the
    # decision-log table. NULL means the turn was a plain text
    # response (no tool invocations); the per-turn hook treats NULL
    # as the explicit "no tool this turn" signal. 128 chars is well
    # above the longest observed tool name (MCP-prefixed identifiers
    # like ``mcp__leartech-criteria__run_criteria_set``).
    last_tool_call: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f'InitiativeRunRow(id={self.id!r}, status={self.status!r})'


class AgentRunDecisionRow(Base):
    """One row per notable agent-loop decision (Layer 2 — decision log).

    Operators query this table to reconstruct WHAT the agent did
    turn-by-turn after a failure, without pod-log archaeology. The
    typical access pattern is

        SELECT turn_index, kind, summary
        FROM agent_run_decisions
        WHERE run_id = 'X'
        ORDER BY turn_index, id;

    so ``run_id`` is indexed and rows are ordered by INSERT time within
    a turn (the BIGSERIAL ``id`` provides that secondary ordering).

    ``kind`` is a short discriminator (``tool_call`` / ``decision`` /
    ``wait`` / ``gate`` / ``terminate`` / ``sigterm`` / etc.). Kept
    free-form string rather than an Enum so adding a new kind doesn't
    require a migration; the value set is documented in
    ``gate.agent.diagnostics``.

    ``payload`` is optional JSON — tool inputs/outputs, gate verdict
    summary, etc. Kept opaque at this layer; consumers parse on read.
    """

    __tablename__ = 'agent_run_decisions'

    # ``with_variant`` lets SQLAlchemy map this to BIGINT on Postgres (the
    # production target for BIGSERIAL ids) and plain INTEGER on SQLite
    # (where INTEGER PRIMARY KEY is the only column type that auto-bumps
    # via rowid). Without this, the SQLite test bootstrap inserts NULL
    # into a BIGINT PK and hits a NOT NULL constraint failure.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, 'sqlite'),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey('initiative_runs.id', ondelete='CASCADE'),
        nullable=False,
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Any | None] = mapped_column(JSONBOrJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index('ix_agent_run_decisions_run_id', 'run_id'),
        Index('ix_agent_run_decisions_created_at', created_at.desc()),
    )

    def __repr__(self) -> str:
        return f'AgentRunDecisionRow(run_id={self.run_id!r}, turn={self.turn_index}, kind={self.kind!r})'


class AgentRunSnapshotRow(Base):
    """Full SDK conversation history per terminal run (Layer 3 — snapshot).

    One row per run (``run_id`` is the primary key). ``messages`` is
    the verbatim list of SDK messages — each entry is a dict with at
    minimum ``{role, content_summary, ...}``. The diagnostics module is
    responsible for shape-normalising SDK message objects into JSON
    before persisting; this column stays opaque to the schema.

    ``message_count`` is denormalised so operators can run
    ``SELECT run_id, message_count, terminal_reason FROM
    agent_run_snapshots ORDER BY created_at DESC LIMIT 20`` without
    parsing JSONB.

    ``terminal_reason`` is the same vocabulary as
    ``initiative_runs.error`` — kept here as well so operators can
    pivot from the snapshot view to the run view without a join.

    ``updated_at`` distinct from ``created_at`` accommodates the
    SIGTERM+normal-terminal race: if the natural terminal write fired
    first, the SIGTERM handler's UPSERT updates the same row in place
    rather than failing on the PK conflict (handled at the CRUD layer
    via ``ON CONFLICT DO UPDATE``).
    """

    __tablename__ = 'agent_run_snapshots'

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey('initiative_runs.id', ondelete='CASCADE'),
        primary_key=True,
    )
    messages: Mapped[Any] = mapped_column(JSONBOrJSON, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (Index('ix_agent_run_snapshots_created_at', created_at.desc()),)

    def __repr__(self) -> str:
        return (
            f'AgentRunSnapshotRow(run_id={self.run_id!r}, '
            f'message_count={self.message_count}, reason={self.terminal_reason!r})'
        )


# Command-type vocabulary for the bidirectional command queue. Mirrored
# at the DB level by the ``CheckConstraint`` below + the
# ``CHECK (command_type IN (...))`` clause in migration 0007 — three
# checks defend in depth so a typo from any of the three writer paths
# (REST endpoint, CLI, raw SQL) is rejected.
AGENT_RUN_COMMAND_TYPES: frozenset[str] = frozenset(
    {'cancel', 'pause', 'resume', 'inject_guidance'},
)


class AgentRunCommandRow(Base):
    """One row per operator-issued command targeting a running agent.

    Implements the v1 bidirectional control surface from initiative
    ``agent-add-command-queue-with-injection``. Today an initiative run
    is fire-and-forget — once spawned the operator can only watch. This
    table is the agent's inbox: between turns the SDK loop polls for
    unacked rows whose ``run_id`` matches its own, processes them in
    submission order, and writes ``acked_at`` + an ``ack_message`` back.

    Command vocabulary (CHECK-enforced at the DB layer; see
    :data:`AGENT_RUN_COMMAND_TYPES` for the in-process duplicate):

      ``cancel``           graceful shutdown. ``payload.reason`` is
                           surfaced as the failure reason in
                           ``initiative_runs.error`` (per Layer 1 of
                           comprehensive failure diagnostics) and a
                           snapshot is persisted before exit.

      ``pause``            agent waits at the next turn boundary for a
                           matching ``resume`` row.

      ``resume``           releases a paused agent.

      ``inject_guidance``  ``payload.text`` is appended to the
                           conversation as a UserMessage so the model
                           sees it as if the operator had spoken.

    The hot read path is ``SELECT * FROM agent_run_commands WHERE
    run_id = ? AND acked_at IS NULL ORDER BY submitted_at`` — covered
    by the partial index defined in migration 0007. SQLite (tests)
    silently degrades the WHERE clause to a regular index; the query
    plan is fine on either backend.
    """

    __tablename__ = 'agent_run_commands'

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, 'sqlite'),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey('initiative_runs.id', ondelete='CASCADE'),
        nullable=False,
    )
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[Any | None] = mapped_column(JSONBOrJSON, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "command_type IN ('cancel', 'pause', 'resume', 'inject_guidance')",
            name='ck_agent_run_commands_command_type',
        ),
        # Non-partial fallback index — covers the same query shape but
        # without the partial predicate (which SQLite's CREATE INDEX
        # syntax does support, but the SQLAlchemy ORM-level Index
        # surface keeps things portable across both engines). The
        # Postgres migration (0007) provides the partial variant in
        # production; here we declare the secondary form so SQLite
        # tests still get an index for the same predicate.
        Index('ix_agent_run_commands_run_id_submitted_at', 'run_id', 'submitted_at'),
    )

    def __repr__(self) -> str:
        return (
            f'AgentRunCommandRow(id={self.id!r}, run_id={self.run_id!r}, '
            f'type={self.command_type!r}, acked={self.acked_at is not None})'
        )
