"""SQLAlchemy 2.0 models — DB-backed initiative catalogue.

One table for now: `initiative_catalog`. Stores the raw YAML so the schema
is evolution-free — the loader parses on read using the same pydantic model
that consumes filesystem YAML. Trades query convenience for schema stability.

If future endpoints need filtering by repo/branch/etc., parse the YAML in
the application layer or add a generated column. Don't normalise.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
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
