"""Preview namespace deployment health.

Currently a placeholder that skips when no preview convention is registered for the repo.
Wired for real once the per-repo preview map lands in `gate/config/repos.yaml` (v1.5).
"""

from __future__ import annotations

import pytest

from gate.tools import PRContext

pytestmark = pytest.mark.shared


def test_preview_namespace_healthy(pr_context: PRContext) -> None:
    """The preview namespace for this PR has a healthy rollout on at least one cluster.

    v1 stub — wire to kubectl rollout status once the per-repo preview namespace pattern
    is configurable. For auth-ui the convention is `jx-mikelear-auth-ui-pr-{N}` per
    preview-shift-left, but the leading `jx-mikelear-` prefix is not universal.
    """
    pytest.skip(
        f'preview-health criterion not yet wired (PR #{pr_context.number}); '
        'needs per-repo namespace pattern config — tracked as v1.5 follow-up'
    )
