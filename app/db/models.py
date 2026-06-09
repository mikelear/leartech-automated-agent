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

from sqlalchemy import DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f'InitiativeRunRow(id={self.id!r}, status={self.status!r})'
