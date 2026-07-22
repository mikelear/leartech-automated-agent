"""Security-claim-in-diff criterion.

If a PR introduces a comment or documentation line claiming that a
security-sensitive feature is "protected by X in production" — where X is a
NetworkPolicy, auth middleware, RBAC binding, or similar guard — the criterion
fails **unless** one of:

  (a) the diff itself introduces the guard (a Kubernetes manifest of the
      matching kind, auth-middleware wire-up, RBAC role binding), OR
  (b) the claim's context / PR title / PR body references an existing manifest
      that can be verified to be present at the PR's head SHA.

Motivation — PR-time paper trail for security claims
-----------------------------------------------------
Chart flips have shipped enabling security-sensitive endpoints with
confident chart-comments claiming NetworkPolicy protection — while the
NetworkPolicy manifest was neither added in the diff nor referenced
anywhere the reviewer could verify. The claim was structurally untrue at
merge-time. The gate catches the paper-trail gap at PR-time so a reviewer
sees the missing guard immediately, not after the change ships.
"""

from __future__ import annotations

import pytest

from gate.tools import (
    PRContext,
    evidence_for_claim,
    fetch_pr_diff,
    parse_security_claims,
)

pytestmark = pytest.mark.shared


def test_security_claim_backed_by_evidence(pr_context: PRContext) -> None:
    """Every security-sensitive protection claim must have matching evidence."""
    diff = fetch_pr_diff(pr_context.qualified_repo, pr_context.number)
    claims = parse_security_claims(diff)
    if not claims:
        pytest.skip(f'PR #{pr_context.number} adds no security-sensitive protection claims in comments/docs.')

    failures: list[str] = []
    for claim in claims:
        ok, reason = evidence_for_claim(
            claim,
            diff,
            pr_context.title,
            pr_context.body,
            pr_context.qualified_repo,
            pr_context.head_sha,
        )
        if not ok:
            failures.append(
                f'{claim.source_file}: `{claim.claim_snippet}` (claim type: {claim.claim_type})\n  {reason}'
            )

    assert not failures, (
        'PR asserts security guards in comments/docs without landing evidence '
        'of the guard:\n\n' + '\n\n'.join(failures) + '\n\n'
        'Fix by either (a) adding the guard in this PR — e.g. the '
        '``NetworkPolicy`` manifest, ``add_middleware(AuthenticationMiddleware, …)`` '
        'wire-up, or RBAC ``RoleBinding`` — or (b) citing an existing manifest '
        'path (e.g. ``charts/<chart>/templates/networkpolicy.yaml``) in a '
        "nearby comment or in the PR's title/body so the reviewer can verify "
        'the claim.'
    )
