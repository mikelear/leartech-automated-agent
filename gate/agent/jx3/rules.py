"""JX3 flow rules.

Shared by:
- `gate/mcp_servers/jx3_flow.py` — runtime query tool
- `gate/agent/calibrations/jx3-full-flow.md` — human-readable spec
  (references this module by path; keep them in sync)

The single source of truth: if a rule changes, change it HERE first,
then mirror to the markdown. Drift is detectable by the test
`tests/test_jx3_calibration_matches_rules.py`.

The PR snapshot is intentionally small and stable — it covers what the
MCP server can populate from the GitHub REST API (PR + check-runs) plus
the merged flag. Stages past `pr_merged_releasing` (release pipeline,
GitOps promotion, cluster rollout) are placeholders for a future expansion
of the snapshot shape that includes Tekton state and GitOps-PR labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

CheckState = Literal['success', 'failure', 'pending', 'neutral']


class JX3Stage(StrEnum):
    """Where a PR is in the leartech JX3 promotion flow.

    Order corresponds to the *forward* progression of a PR; classify_stage
    picks the first matching condition, mirroring how a real PR moves
    through these states monotonically (modulo failure-and-retry loops).
    """

    PR_OPEN_BUILDING = 'pr_open_building'  # PR open; PR-time checks running
    PR_CHECKS_FAILING = 'pr_checks_failing'  # at least one required check is fail
    PR_AWAITING_APPROVAL = 'pr_awaiting_approval'  # all checks pass, no approved label
    PR_HELD = 'pr_held'  # approved + checks but do-not-merge/hold set
    PR_READY_TO_MERGE = 'pr_ready_to_merge'  # approved, no hold, all checks green
    PR_MERGED_RELEASING = 'pr_merged_releasing'  # merged; release running on main
    RELEASE_BUILDING = 'release_building'  # kaniko build / push to cluster registries
    GITOPS_PR_OPEN = 'gitops_pr_open'  # jx-promote opened auto-promote PR
    ROLLED_TO_CLUSTER = 'rolled_to_cluster'  # jx-boot reconciled; new chart live
    UNKNOWN = 'unknown'  # state can't be determined (treat as probe failure)


@dataclass(frozen=True)
class PRSnapshot:
    """Inputs the rules consume.

    Stable shape; never grows past what the MCP server can populate from
    GitHub REST (PR + check-runs) plus the optional lighthouse-keeper
    /merge endpoint.
    """

    labels: frozenset[str]
    checks: dict[str, CheckState]  # name -> state
    merged: bool
    head_sha: str
    mergeable: bool | None = None  # GitHub's mergeable hint; None until computed


# Required-checks list comes from BRANCH PROTECTION — but every cluster
# check is prefixed by its cluster slug (`gcp/...` or `az/...`), which is
# the heuristic we use here. Non-cluster checks (e.g. GitHub-side advisory
# scanners) don't gate merge, so they are ignored.
REQUIRED_CHECKS_PREFIX = ('gcp/', 'az/')


# Chatops scope label — the location where the LLM/operator posts the
# command. Kept as a constant so the rules + tests + docs stay aligned.
_PR_COMMENT_SCOPE = 'PR comment'


def classify_stage(snap: PRSnapshot) -> JX3Stage:
    """Determine the current JX3 stage from a PR snapshot.

    Order matters — the first matching condition wins, since stages
    progress strictly forward.
    """
    if snap.merged:
        # Stage progression past merge needs release-pipeline introspection
        # (separate concern — return PR_MERGED_RELEASING; the MCP server
        # extends with release stage if/when it integrates Tekton state).
        return JX3Stage.PR_MERGED_RELEASING

    cluster_check_states = [state for name, state in snap.checks.items() if name.startswith(REQUIRED_CHECKS_PREFIX)]
    any_fail = 'failure' in cluster_check_states
    any_pending = 'pending' in cluster_check_states
    all_green = bool(cluster_check_states) and all(s == 'success' for s in cluster_check_states)

    if any_fail:
        return JX3Stage.PR_CHECKS_FAILING
    if any_pending:
        return JX3Stage.PR_OPEN_BUILDING
    if not all_green:
        # No required cluster checks have reported yet, OR all are neutral.
        # Treat as "still building" — the gate hasn't spoken yet.
        return JX3Stage.PR_OPEN_BUILDING

    # All required cluster checks are success at this point.
    has_approved = 'approved' in snap.labels
    has_hold = 'do-not-merge/hold' in snap.labels

    if not has_approved:
        return JX3Stage.PR_AWAITING_APPROVAL
    if has_hold:
        return JX3Stage.PR_HELD
    return JX3Stage.PR_READY_TO_MERGE


def blocking_predicate(snap: PRSnapshot) -> str | None:
    """What's preventing merge RIGHT NOW.

    Returns None if Tide would merge the PR as soon as it polls. The
    human-readable string is intended for LLM prompts / sticky comments.
    """
    stage = classify_stage(snap)
    if stage in (JX3Stage.PR_MERGED_RELEASING, JX3Stage.PR_READY_TO_MERGE):
        return None
    if stage == JX3Stage.PR_CHECKS_FAILING:
        red = sorted(n for n, s in snap.checks.items() if s == 'failure')
        return f'Required check(s) failing: {", ".join(red)}'
    if stage == JX3Stage.PR_OPEN_BUILDING:
        pending = sorted(n for n, s in snap.checks.items() if s == 'pending')
        if pending:
            return f'Check(s) still running: {", ".join(pending)}'
        return 'No required cluster checks have reported yet.'
    if stage == JX3Stage.PR_AWAITING_APPROVAL:
        return 'Missing `approved` label. Lighthouse approve plugin applies it when all required checks pass.'
    if stage == JX3Stage.PR_HELD:
        return '`do-not-merge/hold` label set. Post `/hold cancel` to remove.'
    return 'Unknown state — agent should investigate manually.'


def required_actions(snap: PRSnapshot) -> list[dict[str, str]]:
    """Recommended chatops commands the operator or Orch should run.

    Each entry: ``{command, scope, why}``. Empty list = nothing to do
    (PR is merging or merged, or already in the "Tide will land it"
    state).
    """
    stage = classify_stage(snap)
    if stage == JX3Stage.PR_CHECKS_FAILING:
        # /test <name> per failed check; /retest is preferred when MULTIPLE
        # checks are red (single bulk action vs. one per check).
        red = [name.split('/', 1)[-1] for name, state in snap.checks.items() if state == 'failure']
        unique_red = sorted(set(red))
        if len(unique_red) > 1:
            return [
                {
                    'command': '/retest',
                    'scope': _PR_COMMENT_SCOPE,
                    'why': f'{len(unique_red)} required checks failing — bulk retest',
                }
            ]
        return [
            {
                'command': f'/test {unique_red[0]}',
                'scope': _PR_COMMENT_SCOPE,
                'why': (f'Single failed check ({unique_red[0]}); /test fires a fresh run on both clusters'),
            }
        ]
    if stage == JX3Stage.PR_HELD:
        return [
            {
                'command': '/hold cancel',
                'scope': _PR_COMMENT_SCOPE,
                'why': 'Hold label set; Tide blocked until removed',
            }
        ]
    if stage == JX3Stage.PR_AWAITING_APPROVAL:
        return [
            {
                'command': '(wait)',
                'scope': 'noop',
                'why': (
                    'Lighthouse approve plugin auto-applies `approved` when '
                    'all required checks are green. No operator action needed '
                    'if checks are all success.'
                ),
            }
        ]
    return []
