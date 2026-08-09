"""Tests for gate.agent.release_checks — the thin deterministic release-verify checks.

These pin the CONTRACT the PlanTemplate relies on: each check maps to ONE Go MCP tool,
the tool's typed structured result (not narration) decides PASS/FAIL, inputs are the
consolidated {clusters, version} shape, and cluster-local tools reach each cluster via its
own endpoint (no silent double-probe). No network: a fake tool-caller is injected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from gate.agent import release_checks
from gate.agent.release_checks import CheckResult, is_check_action, run_check, run_check_action


@pytest.fixture(autouse=True)
def _base_mcp_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every check needs a resolvable MCP endpoint. Default all clusters to one base;
    tests that exercise per-cluster routing override LEARTECH_MCP_URL_<CLUSTER>."""
    monkeypatch.setenv('LEARTECH_MCP_URL', 'http://mcp')
    monkeypatch.delenv('LEARTECH_MCP_URL_GCP', raising=False)
    monkeypatch.delenv('LEARTECH_MCP_URL_AZ', raising=False)


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def _caller_for(
    fn: Callable[[str, str, str, dict[str, Any]], tuple[dict[str, Any], str | None]],
) -> release_checks.ToolCaller:
    """Wrap a sync scripted function as the async ToolCaller seam."""

    async def _call(base_url: str, server: str, tool: str, args: dict[str, Any]):
        return fn(base_url, server, tool, args)

    return _call


# ── is_check_action ──────────────────────────────────────────────────────────
def test_is_check_action_owns_the_four_actions() -> None:
    for a in ('release-pipeline-status', 'promote-status', 'deploy-health', 'bootjob-for-commit'):
        assert is_check_action(a)
    assert not is_check_action('register-source-config')
    assert not is_check_action('release-health-check')  # legacy LLM action, not ours


# ── release-pipeline-status (tekton) ───────────────────────────────────────────
def test_release_pipeline_fired_and_passed_is_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_MCP_URL_GCP', 'http://gcp')
    monkeypatch.setenv('LEARTECH_MCP_URL_AZ', 'http://az')

    def fn(base, server, tool, args):
        assert server == 'tekton' and tool == 'release_pipeline_status'
        assert args == {'repo': 'mikelear/plan-api', 'sha': 'abc'}
        return {'fired': True, 'passed': True, 'failed': False, 'run': {'name': 'r-1'}}, None

    res: CheckResult = _run(
        run_check(
            'release-pipeline-status',
            {'repo': 'mikelear/plan-api', 'sha': 'abc', 'clusters': ['gcp', 'az']},
            caller=_caller_for(fn),
        )
    )
    assert res.verdict == 'PASS'
    assert [c.cluster for c in res.clusters] == ['gcp', 'az']
    assert all(c.verdict == 'PASS' for c in res.clusters)


def test_release_pipeline_failed_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_MCP_URL_GCP', 'http://gcp')
    monkeypatch.setenv('LEARTECH_MCP_URL_AZ', 'http://az')

    def fn(base, server, tool, args):
        failed = base == 'http://az'
        return {'fired': True, 'passed': not failed, 'failed': failed, 'run': {'name': 'r'}}, None

    res = _run(
        run_check(
            'release-pipeline-status',
            {'repo': 'r', 'sha': 's', 'clusters': ['gcp', 'az']},
            caller=_caller_for(fn),
        )
    )
    assert res.verdict == 'FAIL'
    assert 'az' in res.reason and 'FAILED' in res.reason


def test_release_pipeline_not_fired_is_fail() -> None:
    def fn(base, server, tool, args):
        return {'fired': False, 'passed': False, 'failed': False}, None

    res = _run(run_check('release-pipeline-status', {'repo': 'r', 'sha': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'did not fire' in res.reason


# ── deploy-health (k8s) — version-aware + version-blind ─────────────────────────
def test_deploy_health_healthy_and_version_match_is_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_MCP_URL_GCP', 'http://gcp')

    def fn(base, server, tool, args):
        assert server == 'k8s' and tool == 'deploy_health'
        assert args['expected_version'] == '0.0.14'  # standardized single `version` field
        return {'healthy': True, 'available_replicas': 2, 'observed_version': '0.0.14', 'version_match': True}, None

    res = _run(
        run_check(
            'deploy-health',
            {'service': 'plan-api', 'version': '0.0.14', 'clusters': ['gcp']},
            caller=_caller_for(fn),
        )
    )
    assert res.verdict == 'PASS'


def test_deploy_health_version_mismatch_is_fail() -> None:
    def fn(base, server, tool, args):
        return {'healthy': True, 'available_replicas': 2, 'observed_version': '0.0.13', 'version_match': False}, None

    res = _run(run_check('deploy-health', {'service': 's', 'version': '0.0.14', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'version mismatch' in res.reason


def test_deploy_health_unhealthy_is_fail() -> None:
    def fn(base, server, tool, args):
        return {'healthy': False, 'available_replicas': 0, 'reason': 'ImagePullBackOff'}, None

    res = _run(run_check('deploy-health', {'service': 's', 'version': '0.0.1', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'unhealthy' in res.reason


def test_deploy_health_derives_new_version_from_promote_status() -> None:
    # No explicit `version` → deploy-health calls promote_status, learns the promoted
    # version per cluster, and asserts the RUNNING image equals it.
    def fn(base, server, tool, args):
        if tool == 'promote_status':
            return {'all_merged': True, 'clusters': [{'cluster': 'gcp', 'merged': True, 'version': '0.0.14', 'pr_number': 1348}]}, None
        assert args.get('expected_version') == '0.0.14'  # derived version threaded into deploy_health
        return {'healthy': True, 'available_replicas': 2, 'observed_version': '0.0.14', 'version_match': True}, None

    res = _run(run_check('deploy-health', {'service': 'plan-api', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'PASS'
    assert '0.0.14' in res.clusters[0].reason


def test_deploy_health_no_derivable_version_fails_closed() -> None:
    # promote_status returns no version and none is supplied → REFUSE a version-blind
    # pass (the boot-died-early/old-version-still-up blind spot must not go green).
    def fn(base, server, tool, args):
        if tool == 'promote_status':
            return {'clusters': [{'cluster': 'gcp', 'merged': True}]}, None  # no version field
        return {'healthy': True, 'available_replicas': 1, 'observed_version': 'old', 'version_match': True}, None

    res = _run(run_check('deploy-health', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'version-blind' in res.clusters[0].reason.lower() or 'cannot assert' in res.clusters[0].reason.lower()


# ── promote-status (jx_release) — native cross-cluster ─────────────────────────
def test_promote_status_all_merged_is_pass() -> None:
    def fn(base, server, tool, args):
        assert server == 'jx_release' and tool == 'promote_status'
        assert args == {'service': 'plan-api', 'clusters': ['gcp', 'az']}
        return {
            'all_merged': True,
            'any_gate_failed': False,
            'clusters': [
                {'cluster': 'gcp', 'found': True, 'merged': True, 'pr_number': 1348},
                {'cluster': 'az', 'found': True, 'merged': True, 'pr_number': 1152},
            ],
        }, None

    res = _run(run_check('promote-status', {'service': 'plan-api', 'clusters': ['gcp', 'az']}, caller=_caller_for(fn)))
    assert res.verdict == 'PASS'
    assert len(res.clusters) == 2 and all(c.verdict == 'PASS' for c in res.clusters)


def test_promote_status_gate_failed_is_fail() -> None:
    def fn(base, server, tool, args):
        return {
            'all_merged': False,
            'any_gate_failed': True,
            'clusters': [{'cluster': 'gcp', 'found': True, 'gate_failed': True, 'pr_number': 11}],
        }, None

    res = _run(run_check('promote-status', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'qa-gate FAILED' in res.reason


def test_promote_status_no_pr_yet_is_fail() -> None:
    def fn(base, server, tool, args):
        return {'all_merged': False, 'clusters': [{'cluster': 'gcp', 'found': False}]}, None

    res = _run(run_check('promote-status', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'no promote PR' in res.reason


# ── bootjob-for-commit (k8s) — locked step-7 semantics ─────────────────────────
def test_bootjob_completed_is_pass() -> None:
    def fn(base, server, tool, args):
        assert server == 'k8s' and tool == 'bootjob_for_commit'
        return {'found': True, 'succeeded': True, 'running': False, 'failed': False, 'job_name': 'jx-boot-x'}, None

    res = _run(run_check('bootjob-for-commit', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'PASS'


def test_bootjob_ran_but_job_failed_is_still_pass_housekeeping() -> None:
    # A boot that applied the commit then failed on post-apply housekeeping RAN → PASS.
    def fn(base, server, tool, args):
        return {'found': True, 'succeeded': False, 'running': False, 'failed': True, 'job_name': 'jx-boot-y'}, None

    res = _run(run_check('bootjob-for-commit', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'PASS'
    assert 'housekeeping' in res.clusters[0].reason


def test_bootjob_not_found_is_fail() -> None:
    def fn(base, server, tool, args):
        return {'found': False}, None

    res = _run(run_check('bootjob-for-commit', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'


def test_bootjob_found_but_indeterminate_is_fail() -> None:
    # found but no terminal signal — must NOT be assumed completed (old blind spot).
    def fn(base, server, tool, args):
        return {'found': True, 'succeeded': False, 'running': False, 'failed': False, 'job_name': 'jx-boot-q'}, None

    res = _run(run_check('bootjob-for-commit', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'indeterminate' in res.clusters[0].reason


def test_bootjob_running_is_fail() -> None:
    def fn(base, server, tool, args):
        return {'found': True, 'running': True, 'job_name': 'jx-boot-z'}, None

    res = _run(run_check('bootjob-for-commit', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'still running' in res.reason


# ── input consolidation + endpoint routing ─────────────────────────────────────
def test_missing_required_input_fails_closed() -> None:
    def fn(base, server, tool, args):  # should never be called
        raise AssertionError('caller invoked despite missing input')

    # release-pipeline-status needs repo+sha; omit sha.
    code = _run(run_check_action('release-pipeline-status', {'repo': 'r', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert code == 1


def test_shared_endpoint_fails_closed_no_blind_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # No per-cluster URLs → both clusters resolve to the same endpoint. The second cluster
    # is NOT actually verified, so it must FAIL (not be silently skipped-and-passed).
    # This is the whole-cluster blind spot: a green check that only checked one cluster.
    monkeypatch.delenv('LEARTECH_MCP_URL_GCP', raising=False)
    monkeypatch.delenv('LEARTECH_MCP_URL_AZ', raising=False)
    monkeypatch.setenv('LEARTECH_MCP_URL', 'http://single')
    calls: list[str] = []

    def fn(base, server, tool, args):
        calls.append(base)
        return {'healthy': True, 'available_replicas': 1, 'observed_version': '0.0.1', 'version_match': True}, None

    res = _run(run_check('deploy-health', {'service': 's', 'version': '0.0.1', 'clusters': ['gcp', 'az']}, caller=_caller_for(fn)))
    assert len(calls) == 1  # probed ONCE, not twice
    verdicts = {c.cluster: c.verdict for c in res.clusters}
    assert verdicts['gcp'] == 'PASS' and verdicts['az'] == 'FAIL'  # az NOT silently passed
    assert res.verdict == 'FAIL'  # fail-closed on missing coverage


def test_tool_error_is_fail_not_crash() -> None:
    def fn(base, server, tool, args):
        return {}, 'downstream MCP call failed: timeout'

    res = _run(run_check('deploy-health', {'service': 's', 'version': '0.0.1', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert res.verdict == 'FAIL'
    assert 'call failed' in res.reason


def test_run_check_action_exit_codes() -> None:
    def ok(base, server, tool, args):
        return {'found': True, 'succeeded': True, 'job_name': 'j'}, None

    def bad(base, server, tool, args):
        return {'found': False}, None

    assert _run(run_check_action('bootjob-for-commit', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(ok))) == 0
    assert _run(run_check_action('bootjob-for-commit', {'service': 's', 'clusters': ['gcp']}, caller=_caller_for(bad))) == 1


# ── release-verify POLL behaviour (the "watch the release through" fix) ──────────
def test_release_check_polls_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient FAIL (release still running) is re-checked until it reaches a
    terminal PASS — the check WAITS for the stage instead of one-shot-failing."""
    import gate.agent.release_checks as rc

    monkeypatch.setattr(rc, 'POLL_INTERVAL_S', 0)  # no real sleeping in the test
    calls = {'n': 0}

    def fn(base: str, server: str, tool: str, args: dict) -> tuple[dict, None]:
        calls['n'] += 1
        if calls['n'] == 1:  # first look: fired but still running → transient
            return {'fired': True, 'passed': False, 'failed': False, 'running': True, 'run': {'name': 'r-1'}}, None
        return {'fired': True, 'passed': True, 'failed': False, 'run': {'name': 'r-1'}}, None  # then passed

    code = _run(run_check_action('release-pipeline-status', {'repo': 'r', 'sha': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert code == 0  # polled through to PASS
    assert calls['n'] >= 2  # it did NOT one-shot — it re-checked after "still running"


def test_release_check_poll_times_out_without_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistently transient stage FAILs (not hangs) once the budget is exhausted."""
    import gate.agent.release_checks as rc

    monkeypatch.setattr(rc, 'POLL_BUDGET_S', 0)  # deadline == now → single shot, no wait

    def fn(base: str, server: str, tool: str, args: dict) -> tuple[dict, None]:
        return {'fired': True, 'passed': False, 'failed': False, 'running': True, 'run': {'name': 'r-1'}}, None

    code = _run(run_check_action('release-pipeline-status', {'repo': 'r', 'sha': 's', 'clusters': ['gcp']}, caller=_caller_for(fn)))
    assert code == 1  # transient but out of budget → FAIL (timeout), never hangs


def test_single_cluster_default_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit clusters, the check verifies only the LOCAL cluster
    (LEARTECH_CLUSTER) — no cross-cluster fan-out."""
    import gate.agent.release_checks as rc

    monkeypatch.setenv('LEARTECH_CLUSTER', 'gcp')
    assert rc._resolve_clusters({}) == ('gcp',)
    # explicit clusters still honoured
    assert rc._resolve_clusters({'clusters': ['az']}) == ('az',)
