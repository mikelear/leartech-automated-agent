"""Initiative YAML loader.

Initiatives are declarative descriptions of "make a change in (one or more) repos
to satisfy goal G, gate the result on criteria set C". The agent loop reads one
and drives it end-to-end.

## Two shapes — both supported

**Legacy single-repo (still works)**:

    name: foo
    repo: leartech-auth-ui
    branch: agent/foo
    base: main
    goal: ...

**New multi-repo (preferred for cross-repo work)**:

    name: foo
    repos:
      - repo: webcoder-service
        branch: agent/wire-tenant-list-api
        base: main
      - repo: webcoder-ui
        branch: agent/add-tenant-list-page
        base: main
    goal: ...

A `model_validator` normalises the legacy shape into a single-element `repos`
list at parse time, so downstream code only ever has to handle `initiative.repos`.

Multi-repo *execution* (coordinated changes across multiple PRs in one agent
session) is a follow-up slice — schema accepts the shape today; the agent loop
errors with a clear message when `len(repos) > 1` until that slice lands.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RepoTarget(BaseModel):
    """A single (repo, branch, base) tuple within an initiative.

    `repo` may be `<owner>/<name>` or just `<name>` (owner defaults to `mikelear`).
    """

    model_config = ConfigDict(extra='forbid')

    repo: str = Field(min_length=1, description='`<owner>/<name>` or just `<name>` (defaults owner=mikelear).')
    branch: str = Field(
        min_length=1, description='Branch the agent commits + pushes to. Created from `base` if missing.'
    )
    base: str = Field(default='main', description='Branch to fork from when creating `branch`.')

    @property
    def qualified_repo(self) -> str:
        return self.repo if '/' in self.repo else f'mikelear/{self.repo}'


class Initiative(BaseModel):
    """A single deliverable: scope + intent + acceptance for the agent to drive."""

    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, description='Short kebab-case identifier — also the branch suffix.')
    description: str = Field(default='', description='Why this initiative exists. For humans, not the agent.')

    # New canonical shape: list of repo targets.
    repos: list[RepoTarget] = Field(
        default_factory=list,
        description='List of (repo, branch, base) targets. Preferred over legacy single-repo fields.',
    )

    # Legacy single-repo shorthand. If present and `repos` is empty, the validator
    # collects these into a single-element `repos` list. If both are present,
    # validation fails — pick one shape per initiative.
    repo: str | None = Field(
        default=None, description='Legacy single-repo shorthand. Use `repos: [...]` for new initiatives.'
    )
    branch: str | None = Field(default=None, description='Legacy single-repo shorthand.')
    base: str | None = Field(default=None, description='Legacy single-repo shorthand.')

    goal: str = Field(min_length=1, description='What the agent must accomplish. Constraints belong here verbatim.')

    language: str | None = Field(
        default=None,
        description=(
            'Optional language hint, used by Phase E.1 image-routing to dispatch the run to the '
            'right `leartech-agent-<lang>` image. Any string is accepted at parse time — the '
            'image picker (`_pick_image_for_initiative`) decides what values it knows; unknown '
            'or omitted values fall back to repo auto-detection / the default image. Leaving '
            'unset (or empty string) is the safe default.'
        ),
    )

    image: str | None = Field(
        default=None,
        description=(
            'Optional fully-qualified OCI image reference (Phase E.3). When set, this '
            'wins over `language:` (E.2), repo auto-detection (E.1), and the '
            '`LEARTECH_INITIATIVE_DEFAULT_IMAGE` env (D.4.4). Escape hatch for initiatives '
            'that need a specific custom image — e.g. an experimental agent variant, a '
            'pinned-version image, or a debug build — without code changes. Free-form '
            'string; we do not validate the format because operators may target private '
            'mirrors, digest pins (`@sha256:...`), etc. Empty string is normalised to None '
            '(env fallback applies).'
        ),
    )

    @field_validator('language', 'image', mode='before')
    @classmethod
    def _normalise_blank_strings(cls, value: object) -> object:
        """Treat empty/whitespace-only strings as ``None``.

        YAML authors sometimes leave ``language:`` / ``image:`` set to an empty
        value while sketching an initiative. The image picker treats absence and
        empty identically, so normalise here rather than threading a "blank counts
        as None" check through every consumer.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    gate_marks: list[str] = Field(
        default_factory=list,
        description='Pytest marker filter applied to the gate run (e.g. ["unit"]). Empty = run all criteria.',
    )
    max_iterations: int = Field(default=5, ge=1, le=20, description='Hard ceiling on agent loop iterations.')

    @model_validator(mode='after')
    def _normalise_repos(self) -> Initiative:
        legacy_set = bool(self.repo or self.branch or self.base)
        repos_set = bool(self.repos)

        if legacy_set and repos_set:
            raise ValueError(
                'Cannot use both `repos: [...]` (new shape) and legacy `repo:`/`branch:`/`base:` '
                'fields in the same initiative. Pick one.'
            )

        if not (legacy_set or repos_set):
            raise ValueError(
                'Must specify either `repos: [{repo, branch, base?}, ...]` (new) '
                'or `repo:` + `branch:` (legacy single-repo shorthand).'
            )

        if legacy_set:
            if not (self.repo and self.branch):
                raise ValueError('Legacy single-repo shape requires both `repo:` and `branch:`.')
            # Pydantic v2 models are mutable by default; assign normally.
            self.repos = [RepoTarget(repo=self.repo, branch=self.branch, base=self.base or 'main')]

        return self

    @property
    def primary(self) -> RepoTarget:
        """The first (or only) repo target. Use this for single-repo execution paths."""
        return self.repos[0]

    @property
    def is_multi_repo(self) -> bool:
        return len(self.repos) > 1

    @property
    def qualified_repo(self) -> str:
        """Backwards-compat: returns the primary repo qualified with `mikelear/` prefix if needed."""
        return self.primary.qualified_repo

    @property
    def gate_marks_expr(self) -> str:
        """Pytest -m expression — joined with `or` so multiple marks are union-filtered."""
        return ' or '.join(self.gate_marks)


def load_initiative_from_yaml(yaml_body: str) -> Initiative:
    """Parse an initiative from a raw YAML string. Same validation as load_initiative().

    Used by the DB-backed initiative catalog where the YAML is stored as a
    column rather than a file. Sharing this validation keeps DB-stored and
    filesystem-stored initiatives interchangeable.
    """
    data = yaml.safe_load(yaml_body)
    if not isinstance(data, dict):
        raise ValueError(f'Initiative YAML must contain a mapping at the top level (got {type(data).__name__})')
    return Initiative.model_validate(data)


def load_initiative(path: Path | str) -> Initiative:
    """Parse an initiative YAML file. Raises pydantic.ValidationError on schema mismatch."""
    p = Path(path)
    return load_initiative_from_yaml(p.read_text())
