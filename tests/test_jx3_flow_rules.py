"""Unit tests for the JX3 flow rules module.

These exercise `classify_stage`, `blocking_predicate`, and `required_actions`
against handcrafted PRSnapshots. Pure unit — no network, no asyncio.
"""

from __future__ import annotations

from gate.agent.jx3.rules import (
    JX3Stage,
    PRSnapshot,
    blocking_predicate,
    classify_stage,
    required_actions,
)

# ─── classify_stage ────────────────────────────────────────────────────────────


def _snap(
    *,
    labels: tuple[str, ...] = (),
    checks: dict[str, str] | None = None,
    merged: bool = False,
    head_sha: str = 'deadbeef',
) -> PRSnapshot:
    """Build a snapshot with cluster-prefixed checks for tests."""
    return PRSnapshot(
        labels=frozenset(labels),
        checks=dict(checks or {}),  # type: ignore[arg-type]
        merged=merged,
        head_sha=head_sha,
    )


def test_classify_stage_pr_open_building() -> None:
    snap = _snap(checks={'gcp/pr': 'pending', 'az/pr': 'success'})
    assert classify_stage(snap) == JX3Stage.PR_OPEN_BUILDING


def test_classify_stage_pr_open_building_when_no_required_checks_reported_yet() -> None:
    """Brand-new PR — checks list either empty or only neutral entries
    (e.g. github-actions skeleton) — must read as "still building"."""
    snap = _snap(checks={'lint-advisory': 'neutral'})  # no gcp/* or az/* yet
    assert classify_stage(snap) == JX3Stage.PR_OPEN_BUILDING


def test_classify_stage_pr_checks_failing() -> None:
    snap = _snap(checks={'gcp/pr': 'failure', 'az/pr': 'success'})
    assert classify_stage(snap) == JX3Stage.PR_CHECKS_FAILING


def test_classify_stage_failure_wins_over_pending() -> None:
    """If ANY required check failed, the stage is PR_CHECKS_FAILING even
    if other checks are still pending. Tide would not merge regardless."""
    snap = _snap(checks={'gcp/pr': 'failure', 'az/pr': 'pending'})
    assert classify_stage(snap) == JX3Stage.PR_CHECKS_FAILING


def test_classify_stage_pr_awaiting_approval() -> None:
    snap = _snap(checks={'gcp/pr': 'success', 'az/pr': 'success'})
    assert classify_stage(snap) == JX3Stage.PR_AWAITING_APPROVAL


def test_classify_stage_pr_held() -> None:
    snap = _snap(
        labels=('approved', 'do-not-merge/hold'),
        checks={'gcp/pr': 'success', 'az/pr': 'success'},
    )
    assert classify_stage(snap) == JX3Stage.PR_HELD


def test_classify_stage_pr_ready_to_merge() -> None:
    snap = _snap(
        labels=('approved',),
        checks={'gcp/pr': 'success', 'az/pr': 'success'},
    )
    assert classify_stage(snap) == JX3Stage.PR_READY_TO_MERGE


def test_classify_stage_merged() -> None:
    snap = _snap(
        labels=('approved',),
        checks={'gcp/pr': 'success', 'az/pr': 'success'},
        merged=True,
    )
    assert classify_stage(snap) == JX3Stage.PR_MERGED_RELEASING


def test_non_cluster_checks_are_ignored() -> None:
    """A failing advisory check that isn't gcp/* or az/* must NOT trigger
    PR_CHECKS_FAILING — Tide doesn't care about advisory checks."""
    snap = _snap(
        labels=('approved',),
        checks={
            'gcp/pr': 'success',
            'az/pr': 'success',
            'advisory-lint': 'failure',  # not gcp/ or az/ — ignored
        },
    )
    assert classify_stage(snap) == JX3Stage.PR_READY_TO_MERGE


# ─── blocking_predicate ────────────────────────────────────────────────────────


def test_blocking_predicate_ready_to_merge_is_none() -> None:
    snap = _snap(
        labels=('approved',),
        checks={'gcp/pr': 'success', 'az/pr': 'success'},
    )
    assert blocking_predicate(snap) is None


def test_blocking_predicate_merged_is_none() -> None:
    snap = _snap(merged=True)
    assert blocking_predicate(snap) is None


def test_blocking_predicate_held_explains_hold_label() -> None:
    snap = _snap(
        labels=('approved', 'do-not-merge/hold'),
        checks={'gcp/pr': 'success', 'az/pr': 'success'},
    )
    msg = blocking_predicate(snap)
    assert msg is not None
    assert 'do-not-merge/hold' in msg
    assert '/hold cancel' in msg


def test_blocking_predicate_failing_lists_red_checks_alphabetically() -> None:
    snap = _snap(checks={'gcp/pr': 'failure', 'az/end2end': 'failure', 'az/pr': 'success'})
    msg = blocking_predicate(snap)
    assert msg is not None
    # Alphabetical so the message is stable across runs / shuffled dict orders.
    assert 'az/end2end, gcp/pr' in msg


def test_blocking_predicate_awaiting_approval_explains_label_flow() -> None:
    snap = _snap(checks={'gcp/pr': 'success', 'az/pr': 'success'})
    msg = blocking_predicate(snap)
    assert msg is not None
    assert 'approved' in msg


# ─── required_actions ─────────────────────────────────────────────────────────


def test_required_actions_checks_failing_single_returns_test_command() -> None:
    snap = _snap(checks={'gcp/pr': 'failure', 'az/pr': 'success'})
    actions = required_actions(snap)
    assert len(actions) == 1
    assert actions[0]['command'] == '/test pr'
    assert actions[0]['scope'] == 'PR comment'


def test_required_actions_checks_failing_multi_returns_retest() -> None:
    """When MORE than one DISTINCT check name fails, prefer bulk /retest."""
    snap = _snap(checks={'gcp/pr': 'failure', 'az/end2end': 'failure'})
    actions = required_actions(snap)
    assert len(actions) == 1
    assert actions[0]['command'] == '/retest'


def test_required_actions_same_check_on_both_clusters_is_single() -> None:
    """gcp/pr failing AND az/pr failing collapses to the single check name `pr`
    — still one action, /test pr (not /retest), because the agent should
    retrigger that one check rather than bulk-retest everything."""
    snap = _snap(checks={'gcp/pr': 'failure', 'az/pr': 'failure'})
    actions = required_actions(snap)
    assert len(actions) == 1
    assert actions[0]['command'] == '/test pr'


def test_required_actions_held_returns_hold_cancel() -> None:
    snap = _snap(
        labels=('approved', 'do-not-merge/hold'),
        checks={'gcp/pr': 'success', 'az/pr': 'success'},
    )
    actions = required_actions(snap)
    assert len(actions) == 1
    assert actions[0]['command'] == '/hold cancel'


def test_required_actions_awaiting_approval_returns_wait_noop() -> None:
    """Approval is automatic when checks go green — operator action is to wait,
    not to /lgtm or similar. The rules return an explicit (wait) marker so
    the LLM doesn't fabricate a chatops command for this stage."""
    snap = _snap(checks={'gcp/pr': 'success', 'az/pr': 'success'})
    actions = required_actions(snap)
    assert len(actions) == 1
    assert actions[0]['command'] == '(wait)'
    assert actions[0]['scope'] == 'noop'


def test_required_actions_merged_is_empty() -> None:
    snap = _snap(merged=True)
    assert required_actions(snap) == []


def test_required_actions_ready_to_merge_is_empty() -> None:
    snap = _snap(
        labels=('approved',),
        checks={'gcp/pr': 'success', 'az/pr': 'success'},
    )
    assert required_actions(snap) == []


def test_required_actions_open_building_is_empty() -> None:
    """While builds run there's nothing to do — wait is implicit."""
    snap = _snap(checks={'gcp/pr': 'pending'})
    assert required_actions(snap) == []
