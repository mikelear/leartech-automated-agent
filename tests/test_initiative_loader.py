"""Unit tests for the initiative YAML loader.

Covers both shapes:
- Legacy single-repo (`repo:` + `branch:` + `base:` at top level)
- New multi-repo (`repos: [{repo, branch, base?}, ...]`)

Plus the normalisation rules + error cases for using both / neither.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from gate.initiatives.loader import Initiative, RepoTarget, load_initiative


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'init.yaml'
    path.write_text(textwrap.dedent(body))
    return path


# Legacy single-repo shape ────────────────────────────────────────────────────


def test_minimal_valid_initiative_loads(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        """,
    )
    init = load_initiative(path)
    assert init.name == 'x'
    # Legacy fields hold what the user wrote
    assert init.repo == 'leartech-auth-ui'
    assert init.branch == 'agent/x'
    # Normalised value (with default `base: main`) lives on `repos[0]`
    assert init.primary.repo == 'leartech-auth-ui'
    assert init.primary.branch == 'agent/x'
    assert init.primary.base == 'main'  # default applied via normalisation
    assert init.gate_marks == []
    assert init.max_iterations == 5
    assert not init.is_multi_repo


def test_qualified_repo_passes_through_owner() -> None:
    init = Initiative(name='x', repo='someone/leartech-auth-ui', branch='agent/x', goal='g')
    assert init.qualified_repo == 'someone/leartech-auth-ui'


def test_qualified_repo_defaults_owner_to_mikelear() -> None:
    init = Initiative(name='x', repo='leartech-auth-ui', branch='agent/x', goal='g')
    assert init.qualified_repo == 'mikelear/leartech-auth-ui'


def test_gate_marks_expr_joins_with_or() -> None:
    init = Initiative(name='x', repo='r', branch='b', goal='g', gate_marks=['unit', 'playwright'])
    assert init.gate_marks_expr == 'unit or playwright'


def test_gate_marks_expr_empty_when_no_marks() -> None:
    init = Initiative(name='x', repo='r', branch='b', goal='g')
    assert init.gate_marks_expr == ''


# New multi-repo shape ────────────────────────────────────────────────────────


def test_new_repos_shape_loads(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        name: x
        repos:
          - repo: webcoder-service
            branch: agent/wire-tenant-list-api
          - repo: webcoder-ui
            branch: agent/add-tenant-list-page
            base: main
        goal: do a thing
        """,
    )
    init = load_initiative(path)
    assert len(init.repos) == 2
    assert init.repos[0].repo == 'webcoder-service'
    assert init.repos[0].branch == 'agent/wire-tenant-list-api'
    assert init.repos[0].base == 'main'  # default
    assert init.repos[1].repo == 'webcoder-ui'
    assert init.is_multi_repo
    # Legacy fields are None when new shape was used
    assert init.repo is None
    assert init.branch is None
    # `primary` and `qualified_repo` still work
    assert init.primary.repo == 'webcoder-service'
    assert init.qualified_repo == 'mikelear/webcoder-service'


def test_repos_target_qualified_repo_defaults_owner() -> None:
    target = RepoTarget(repo='webcoder-ui', branch='agent/x')
    assert target.qualified_repo == 'mikelear/webcoder-ui'


def test_repos_target_qualified_repo_passes_through_owner() -> None:
    target = RepoTarget(repo='spring-financial-group/mqube-foo', branch='agent/x')
    assert target.qualified_repo == 'spring-financial-group/mqube-foo'


def test_single_repo_in_repos_list_is_not_multi_repo(tmp_path: Path) -> None:
    """A single-element `repos:` list is the new shape but not multi-repo execution."""
    path = _write(
        tmp_path,
        """
        name: x
        repos:
          - repo: leartech-auth-ui
            branch: agent/x
        goal: g
        """,
    )
    init = load_initiative(path)
    assert len(init.repos) == 1
    assert not init.is_multi_repo


# Normalisation error cases ──────────────────────────────────────────────────


def test_using_both_shapes_raises(tmp_path: Path) -> None:
    """Mixing legacy single-repo fields and new `repos:` is a config error — pick one."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        repos:
          - repo: webcoder-ui
            branch: agent/y
        goal: g
        """,
    )
    with pytest.raises(ValidationError, match='Cannot use both'):
        load_initiative(path)


def test_specifying_neither_shape_raises(tmp_path: Path) -> None:
    """Must specify at least one of the two shapes."""
    path = _write(
        tmp_path,
        """
        name: x
        goal: g
        """,
    )
    with pytest.raises(ValidationError, match='Must specify either'):
        load_initiative(path)


def test_legacy_shape_requires_both_repo_and_branch(tmp_path: Path) -> None:
    """`repo:` alone (without `branch:`) is malformed."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        goal: g
        """,
    )
    with pytest.raises(ValidationError, match='requires both `repo:` and `branch:`'):
        load_initiative(path)


def test_extra_fields_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        name: x
        repo: r
        branch: b
        goal: g
        bogus_field: yes
        """,
    )
    with pytest.raises(ValidationError):
        load_initiative(path)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    path = tmp_path / 'init.yaml'
    path.write_text('- just a list\n')
    with pytest.raises(ValueError, match='must contain a mapping'):
        load_initiative(path)


def test_max_iterations_clamped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        name: x
        repo: r
        branch: b
        goal: g
        max_iterations: 50
        """,
    )
    with pytest.raises(ValidationError):
        load_initiative(path)


# Worked-example smoke tests ─────────────────────────────────────────────────


def test_real_worked_example_loads() -> None:
    """Pin the shape of the worked example so accidental edits to it surface here first.

    `auth-ui-home-component-spec.yaml` (the prior pin) was deleted in PR
    #103; switch to `automated-agent-add-changelog-stub.yaml` which is
    still in the catalog and uses the same legacy single-repo shape.
    """
    init = load_initiative(Path(__file__).parent.parent / 'initiatives' / 'automated-agent-add-changelog-stub.yaml')
    assert init.name == 'automated-agent-add-changelog-stub'
    assert init.qualified_repo == 'mikelear/leartech-automated-agent'
    # Worked example uses legacy shape, so `init.branch` (legacy field) holds the branch name
    assert init.branch == 'agent/add-changelog-stub'
    assert init.primary.branch == 'agent/add-changelog-stub'
    assert init.gate_marks == ['unit']


# Optional `language` field ──────────────────────────────────────────────────


def test_language_field_set_when_present(tmp_path: Path) -> None:
    """Parsing a YAML with `language: go` sets the field on the loaded model."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        language: go
        """,
    )
    init = load_initiative(path)
    assert init.language == 'go'


def test_language_field_defaults_to_none_when_omitted(tmp_path: Path) -> None:
    """When `language:` is absent the field is None (not required)."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        """,
    )
    init = load_initiative(path)
    assert init.language is None


def test_language_field_accepts_angular(tmp_path: Path) -> None:
    """Phase E.2: `language: angular` parses cleanly. Many existing YAMLs
    declare this so the field must round-trip without error."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        language: angular
        """,
    )
    init = load_initiative(path)
    assert init.language == 'angular'


def test_language_field_accepts_arbitrary_string(tmp_path: Path) -> None:
    """Phase E.2: parse-time validation is permissive — any string is accepted.

    The image picker decides which values it knows how to route. Unknown
    languages fall back to the default image rather than failing parse, so
    sketch YAMLs with experimental language hints don't refuse to load.
    """
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        language: kotlin
        """,
    )
    init = load_initiative(path)
    assert init.language == 'kotlin'


def test_language_field_empty_string_treated_as_none(tmp_path: Path) -> None:
    """An empty `language: ''` (or whitespace-only) is normalised to None so
    downstream consumers only see one shape for "no language declared"."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        language: ''
        """,
    )
    init = load_initiative(path)
    assert init.language is None


def test_language_field_whitespace_only_treated_as_none(tmp_path: Path) -> None:
    """Whitespace-only language values also normalise to None — same reasoning
    as empty string: YAML authors sometimes leave the value blank-ish while
    sketching, and we don't want the picker to receive a literal '   '."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        language: '   '
        """,
    )
    init = load_initiative(path)
    assert init.language is None
