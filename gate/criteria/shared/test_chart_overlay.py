"""Chart-flip / overlay-PR criterion.

If a PR introduces a new ``*.enabled: false`` toggle in a Helm chart's
``values.yaml`` AND the surrounding chart-comment claims a per-cluster GitOps
overlay flips it on in production, the criterion fails **unless** one of:

  (a) an overlay YAML in a known cluster GitOps repo already sets the matching
      key to a truthy override, OR
  (b) the PR title/body references a linked overlay PR by ``owner/repo#N`` or
      a GitHub PR URL against a cluster repo.

Motivation — PR-time paper trail for chart flips
------------------------------------------------
Agent-authored PRs have shipped chart toggles defaulting to ``false`` with
confident chart-comments claiming "prod overlay opts in" — and no paired
overlay PR ever landed. In production the toggle stays off; the feature
silently never ships. The gate catches the paper-trail gap at PR-time so a
reviewer sees the missing overlay PR immediately, not weeks later when
someone wonders why the feature isn't live.
"""

from __future__ import annotations

import pytest

from gate.tools import (
    PRContext,
    evidence_for_flip,
    fetch_pr_diff,
    parse_chart_flip_signals,
)

pytestmark = pytest.mark.shared


def test_chart_flip_has_overlay_or_linked_pr(pr_context: PRContext) -> None:
    """Every new chart-flip toggle with a prod-overlay hint must have overlay evidence."""
    diff = fetch_pr_diff(pr_context.qualified_repo, pr_context.number)
    signals = parse_chart_flip_signals(diff)
    if not signals:
        pytest.skip(
            f'PR #{pr_context.number} adds no chart-flip toggles whose comments '
            'claim a per-cluster GitOps overlay flip.'
        )

    failures: list[str] = []
    for signal in signals:
        ok, reason = evidence_for_flip(signal, pr_context.title, pr_context.body)
        if not ok:
            failures.append(
                f'{signal.chart_path} → `{signal.dotted_key}: {str(signal.default_value).lower()}`\n  {reason}'
            )

    assert not failures, (
        'Chart introduces prod-overlay-hinted flips with no paired overlay '
        'landing evidence:\n\n' + '\n\n'.join(failures) + '\n\n'
        'Fix by either (a) landing the per-cluster GitOps overlay PR first '
        'and merging it before this PR, or (b) referencing the linked overlay '
        "PR in this PR's title or body (`mikelear/jx-build-cluster-<cluster>#<n>` "
        'or the full GitHub PR URL).'
    )
