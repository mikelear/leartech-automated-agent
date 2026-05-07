"""Auto-skip every criterion in this directory unless the PR's repo is a known
consumer of `leartech-angular-service-template`.

The registry is single-sourced from `gate.tools.triggers.GOLDEN_TEMPLATE_FOR` —
adding a new repo there with `'…/leartech-angular-service-template'` value
automatically picks it up here. No edits to this file needed when a new
angular-template consumer comes online.

Replaces the old `gate/criteria/per_repo/auth_ui/conftest.py` which scoped these
criteria to auth-ui only — that scoping was a v1 limitation surfaced by the
2026-05-05 webcoder-ui dogfood demo (see
`gate/agent/lessons/catalog/per-repo-criteria-must-be-shareable-across-template-consumers.md`).
"""

from __future__ import annotations

import pytest

from gate.tools import PRContext, angular_template_consumers


@pytest.fixture(autouse=True)
def _only_for_angular_template_consumers(pr_context: PRContext) -> None:
    short_name = pr_context.repo.split('/')[-1]
    consumers = angular_template_consumers()
    if short_name not in consumers:
        pytest.skip(f'angular-service-template criterion (got {pr_context.repo}); consumers: {sorted(consumers)}')
