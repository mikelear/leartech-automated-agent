"""Unit tests for gate.tools.pr_context — exercises qualified_repo derivation."""

from __future__ import annotations

from gate.tools.pr_context import PRContext


def _ctx(repo: str) -> PRContext:
    return PRContext(
        repo=repo,
        number=1,
        head_sha='deadbeef',
        base_sha='cafebabe',
        title='t',
        body='',
        changed_files=(),
        state='OPEN',
    )


def test_qualified_repo_passes_through_when_already_qualified() -> None:
    assert _ctx('mikelear/leartech-auth-ui').qualified_repo == 'mikelear/leartech-auth-ui'


def test_qualified_repo_defaults_owner_to_mikelear() -> None:
    assert _ctx('leartech-auth-ui').qualified_repo == 'mikelear/leartech-auth-ui'
