"""Determinism pins for the release-health verdict aggregator.

These tests are the whole point of the fix: the same setup MUST produce the
same verdict every time. Historically the LLM improvised — one run curled the
ingress /health and PASSED, another concluded "kubectl not available" and
FAILED closed. Even after that was moved to an httpx probe the probe itself
was unreachable from the infra sandbox → same cluster-asymmetric FAILs.

The verdict now comes from Python parsing the LLM's per-stage
``STAGE_STATUS:`` lines (emitted after each MCP call) and any early-exit
``RELEASE_HEALTH: FAIL`` line. No httpx probe, no kubectl, no shell-outs.

Key invariants:

* every required (stage, cluster) pair reports PASS → verdict PASS;
* any required pair missing a PASS → verdict FAIL with the missing pair
  named (no PASS-by-silence);
* any explicit STAGE_STATUS FAIL → verdict FAIL naming the first failing
  (stage, cluster) with its reason;
* any explicit ``RELEASE_HEALTH: FAIL: ...`` → verdict FAIL with that reason
  (early-exit short-circuit for gate-fails / stalled promotes);
* a stray ``RELEASE_HEALTH: PASS`` from the LLM is IGNORED — PASS is decided
  by parsed STAGE_STATUS lines, never by the LLM saying so;
* the module NEVER shells out or touches kubectl / httpx (asymmetry closed).
"""

from __future__ import annotations

from gate.agent import release_health
from gate.agent.release_health import (
    DEFAULT_CLUSTERS,
    REQUIRED_STAGES,
    ProbeResult,
    StageVerdict,
    compute_release_health,
    parse_early_exit_fail,
    parse_stage_verdicts,
)

# ── stage-emission builders ───────────────────────────────────────────────────
#
# Small helpers so tests read as *scenarios* rather than newline-string
# soup. Emissions match the exact grammar the LLM is instructed to produce.


def _line(stage: int, cluster: str, verdict: str, reason: str | None = None) -> str:
    base = f'STAGE_STATUS: stage={stage} cluster={cluster} verdict={verdict}'
    if reason:
        return f'{base} reason={reason}'
    return base


def _happy_path_transcript(reason: str = 'ok') -> str:
    """The 7 STAGE_STATUS lines a fully-healthy release emits (stage 1 =
    per-repo, stages 2..4 = per cluster × 2 clusters)."""
    return '\n'.join(
        [
            _line(1, '-', 'PASS', f'release v1.2.3 Succeeded ({reason})'),
            _line(2, 'gcp', 'PASS', 'promote PR #101 merged'),
            _line(2, 'az', 'PASS', 'promote PR #102 merged'),
            _line(3, 'gcp', 'PASS', 'jx-boot Job svc-boot-101 succeeded'),
            _line(3, 'az', 'PASS', 'jx-boot Job svc-boot-102 succeeded'),
            _line(4, 'gcp', 'PASS', 'healthy=true available_replicas=2'),
            _line(4, 'az', 'PASS', 'healthy=true available_replicas=2'),
        ]
    )


# ── the determinism headline ─────────────────────────────────────────────────


def test_all_stages_pass_yields_verdict_pass() -> None:
    """The core fix: every (stage, cluster) PASS → verdict PASS, computed
    WITHOUT any HTTP / kubectl involvement."""
    result = compute_release_health(_happy_path_transcript())
    assert result.verdict == 'PASS'
    assert result.reason is None
    assert result.failing_stage is None
    assert result.failing_cluster is None
    # Every emitted stage is preserved for downstream logging.
    assert len(result.stages) == 7
    assert {(s.stage, s.cluster) for s in result.stages} == {
        (1, '-'),
        (2, 'gcp'),
        (2, 'az'),
        (3, 'gcp'),
        (3, 'az'),
        (4, 'gcp'),
        (4, 'az'),
    }


def test_module_does_not_shell_out_or_touch_kubernetes_or_httpx() -> None:
    """kubectl / httpx are deliberately NOT touched — the cluster-asymmetric
    "sandbox can't reach the ingress" bug is closed by CODE, not docstring.

    Enforced by CODE, not docstring — the module explains why it doesn't touch
    these, which is fine. What we forbid is actually running them: no
    ``subprocess`` import, no kubernetes SDK import, no httpx import.
    """
    import gate.agent.release_health as mod  # noqa: PLC0415 — assertion-scoped

    # No subprocess-based shell-outs (that's how a caller would run kubectl).
    assert not hasattr(mod, 'subprocess')
    # No kubernetes SDK either.
    for attr in ('kubernetes', 'ApiClient', 'CoreV1Api'):
        assert not hasattr(mod, attr), f'release_health should not use {attr!r}'
    # No httpx — the httpx probe was the specific bug this refactor closes.
    assert not hasattr(mod, 'httpx')
    # And the module source doesn't shell out or import forbidden clients.
    # These are USAGE patterns (imports + call syntax) so the docstring can
    # freely explain WHY we don't use them without triggering the check.
    src = (release_health.__file__ or '').replace('.pyc', '.py')
    with open(src) as fh:
        text = fh.read()
    for forbidden in (
        'import subprocess',
        'from subprocess',
        'import httpx',
        'from httpx',
        'import kubernetes',
        'from kubernetes',
        'os.system(',
        'Popen(',
        'subprocess.run(',
    ):
        assert forbidden not in text, f'release_health should not use {forbidden!r}'


# ── STAGE_STATUS parser: parse the exact grammar the LLM must emit ───────────


def test_parse_stage_verdicts_extracts_every_line() -> None:
    transcript = _happy_path_transcript()
    parsed = parse_stage_verdicts(transcript)
    assert len(parsed) == 7
    assert parsed[0] == StageVerdict(stage=1, cluster='-', verdict='PASS', reason='release v1.2.3 Succeeded (ok)')
    assert parsed[-1].stage == 4
    assert parsed[-1].cluster == 'az'


def test_parse_stage_verdicts_ignores_narration_between_lines() -> None:
    """Narrative text between STAGE_STATUS lines is dropped — the aggregator
    only sees structured emissions."""
    transcript = '\n'.join(
        [
            'ok, calling release_status now...',
            _line(1, '-', 'PASS'),
            'now the promote status per cluster',
            _line(2, 'gcp', 'PASS'),
            'AZ was flaky; retesting once, then re-poll',
            _line(2, 'az', 'PASS'),
        ]
    )
    parsed = parse_stage_verdicts(transcript)
    assert [(p.stage, p.cluster) for p in parsed] == [(1, '-'), (2, 'gcp'), (2, 'az')]


def test_parse_stage_verdicts_tolerates_quoted_reason() -> None:
    """LLMs love wrapping strings in quotes; ``reason="..."`` becomes ``reason=...``."""
    transcript = 'STAGE_STATUS: stage=4 cluster=gcp verdict=FAIL reason="deployment not ready"'
    parsed = parse_stage_verdicts(transcript)
    assert parsed[0].reason == 'deployment not ready'


def test_parse_stage_verdicts_drops_malformed_lines() -> None:
    """Unknown verdict / missing fields → dropped silently.

    Coverage requirements later catch the missing pair, so a garbled line
    surfaces as a missing-coverage FAIL rather than a silent PASS.
    """
    transcript = '\n'.join(
        [
            'STAGE_STATUS: stage=4 cluster=gcp verdict=MAYBE',  # bad verdict
            'STAGE_STATUS: cluster=gcp verdict=PASS',  # missing stage
            _line(1, '-', 'PASS'),
            _line(4, 'gcp', 'PASS'),
        ]
    )
    parsed = parse_stage_verdicts(transcript)
    assert len(parsed) == 2
    assert {(p.stage, p.cluster) for p in parsed} == {(1, '-'), (4, 'gcp')}


# ── FAIL paths ────────────────────────────────────────────────────────────────


def test_first_stage_fail_names_the_failing_stage() -> None:
    """A single STAGE_STATUS FAIL flips verdict to FAIL, naming (stage, cluster)."""
    transcript = '\n'.join(
        [
            _line(1, '-', 'PASS'),
            _line(2, 'gcp', 'PASS'),
            _line(2, 'az', 'FAIL', 'qa-gate blocked promote PR #77'),
        ]
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'FAIL'
    assert result.reason is not None
    assert 'stage 2' in result.reason
    assert 'cluster=az' in result.reason
    assert 'qa-gate blocked' in result.reason
    assert result.failing_stage == 2
    assert result.failing_cluster == 'az'


def test_deploy_unhealthy_on_one_cluster_fails() -> None:
    """Stage 4 FAIL on a single cluster names deploy_health details in reason."""
    transcript = '\n'.join(
        [
            _line(1, '-', 'PASS'),
            _line(2, 'gcp', 'PASS'),
            _line(2, 'az', 'PASS'),
            _line(3, 'gcp', 'PASS'),
            _line(3, 'az', 'PASS'),
            _line(4, 'gcp', 'PASS', 'healthy=true available_replicas=2'),
            _line(4, 'az', 'FAIL', 'healthy=false available_replicas=0 desired_replicas=1 deployment not ready'),
        ]
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'FAIL'
    assert result.failing_stage == 4
    assert result.failing_cluster == 'az'
    assert 'healthy=false' in (result.reason or '')


def test_missing_stage_coverage_is_fail() -> None:
    """No PASS-by-silence: a missing required (stage, cluster) pair FAILs
    naming the first gap."""
    # Miss stage 4 az entirely.
    transcript = '\n'.join(
        [
            _line(1, '-', 'PASS'),
            _line(2, 'gcp', 'PASS'),
            _line(2, 'az', 'PASS'),
            _line(3, 'gcp', 'PASS'),
            _line(3, 'az', 'PASS'),
            _line(4, 'gcp', 'PASS'),
            # stage=4 cluster=az is missing.
        ]
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'FAIL'
    assert result.failing_stage == 4
    assert result.failing_cluster == 'az'
    assert 'no STAGE_STATUS PASS emitted' in (result.reason or '')


def test_empty_transcript_is_fail_naming_stage_1() -> None:
    """No STAGE_STATUS lines at all → FAIL naming the first missing stage."""
    result = compute_release_health('nothing happened')
    assert result.verdict == 'FAIL'
    assert result.failing_stage == 1
    assert result.failing_cluster == '-'


def test_stage_1_can_be_reported_per_cluster_or_none() -> None:
    """Stage 1 is per-repo; the LLM may accidentally emit cluster='gcp' on it.
    Either shape (``cluster='-'`` or any required cluster) satisfies coverage."""
    transcript = '\n'.join(
        [
            _line(1, 'gcp', 'PASS', 'release Succeeded'),  # deliberately cluster=gcp
            _line(2, 'gcp', 'PASS'),
            _line(2, 'az', 'PASS'),
            _line(3, 'gcp', 'PASS'),
            _line(3, 'az', 'PASS'),
            _line(4, 'gcp', 'PASS'),
            _line(4, 'az', 'PASS'),
        ]
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'PASS'


# ── early-exit RELEASE_HEALTH: FAIL ──────────────────────────────────────────


def test_early_exit_fail_free_form_shortcircuits() -> None:
    """A ``RELEASE_HEALTH: FAIL: <reason>`` line short-circuits to FAIL even
    when subsequent PASS STAGE_STATUS lines follow (they may be stale
    narrative). LAST FAIL wins."""
    transcript = '\n'.join(
        [
            _line(1, '-', 'PASS'),
            'RELEASE_HEALTH: FAIL: release did not fire within 60min',
            _line(2, 'gcp', 'PASS'),  # ignored — early-exit already wins
        ]
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'FAIL'
    assert 'release did not fire' in (result.reason or '')


def test_early_exit_fail_structured_form_captures_stage_and_cluster() -> None:
    """The structured ``stage=<n> cluster=<c> reason=<r>`` form is preferred —
    the aggregator captures both fields for downstream logs."""
    transcript = (
        'RELEASE_HEALTH: FAIL: '
        'stage=2 cluster=gcp reason=needs-cross-plan-Infra-agent: qa-gate failed on promote PR #123'
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'FAIL'
    assert result.failing_stage == 2
    assert result.failing_cluster == 'gcp'
    assert 'stage 2' in (result.reason or '')
    assert 'cluster=gcp' in (result.reason or '')
    assert 'needs-cross-plan-Infra-agent' in (result.reason or '')


def test_early_exit_last_fail_wins() -> None:
    """Multiple ``RELEASE_HEALTH: FAIL`` lines → the LAST one is the verdict.

    The LLM may narrate a transient early failure it then recovered from.
    Only the final line matters."""
    transcript = '\n'.join(
        [
            'RELEASE_HEALTH: FAIL: transient',
            'ok, recovered',
            'RELEASE_HEALTH: FAIL: final: gcp qa-gate failed',
        ]
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'FAIL'
    assert 'final' in (result.reason or '')


def test_stray_release_health_pass_is_ignored() -> None:
    """A stray ``RELEASE_HEALTH: PASS`` from the LLM is IGNORED — PASS is
    decided by STAGE_STATUS coverage, never by the LLM saying so."""
    # Only stage 1 PASS is emitted — stages 2/3/4 are missing → should FAIL.
    transcript = '\n'.join(
        [
            _line(1, '-', 'PASS'),
            'RELEASE_HEALTH: PASS: I think it looks good',  # ignored
        ]
    )
    result = compute_release_health(transcript)
    assert result.verdict == 'FAIL'  # coverage gap forces FAIL


def test_parse_early_exit_fail_returns_tuple() -> None:
    assert parse_early_exit_fail('nothing') == (None, None, None)
    assert parse_early_exit_fail('RELEASE_HEALTH: FAIL: boom') == (None, None, 'boom')
    stage, cluster, reason = parse_early_exit_fail('RELEASE_HEALTH: FAIL: stage=3 cluster=az reason=jx-boot failed')
    assert stage == 3
    assert cluster == 'az'
    assert reason == 'jx-boot failed'


# ── single-cluster runs (inputs pin one cluster) ─────────────────────────────


def test_single_cluster_run_pins_required_clusters() -> None:
    """A plan step with ``clusters=[gcp]`` requires PASS coverage only on
    that cluster — az's absence must not cause a FAIL."""
    transcript = '\n'.join(
        [
            _line(1, '-', 'PASS'),
            _line(2, 'gcp', 'PASS'),
            _line(3, 'gcp', 'PASS'),
            _line(4, 'gcp', 'PASS'),
        ]
    )
    result = compute_release_health(transcript, required_clusters=('gcp',))
    assert result.verdict == 'PASS'


def test_skip_on_non_required_cluster_is_not_a_fail() -> None:
    """An LLM SKIP for a cluster the aggregator doesn't require does NOT
    cause a FAIL — the aggregator only reasons about required clusters."""
    transcript = '\n'.join(
        [
            _line(1, '-', 'PASS'),
            _line(2, 'gcp', 'PASS'),
            _line(2, 'az', 'SKIP', 'single-cluster run (inputs.cluster=gcp)'),
            _line(3, 'gcp', 'PASS'),
            _line(4, 'gcp', 'PASS'),
        ]
    )
    result = compute_release_health(transcript, required_clusters=('gcp',))
    assert result.verdict == 'PASS'


# ── module constants pinned ──────────────────────────────────────────────────


def test_required_stages_are_1_through_4() -> None:
    """Guard against silent stage drift. Any additions to REQUIRED_STAGES
    need an accompanying prompt update; this pin flags such divergence."""
    assert REQUIRED_STAGES == (1, 2, 3, 4)


def test_default_clusters_are_gcp_and_az() -> None:
    assert DEFAULT_CLUSTERS == ('gcp', 'az')


# ── dataclass shapes ─────────────────────────────────────────────────────────


def test_stage_verdict_as_dict_matches_schema() -> None:
    sv = StageVerdict(stage=4, cluster='gcp', verdict='PASS', reason='healthy=true')
    assert sv.as_dict() == {
        'stage': 4,
        'cluster': 'gcp',
        'verdict': 'PASS',
        'reason': 'healthy=true',
    }


def test_probe_result_as_dict_matches_schema() -> None:
    r = ProbeResult(
        verdict='FAIL',
        reason='stage 4 cluster=az: healthy=false',
        stages=(StageVerdict(stage=4, cluster='az', verdict='FAIL', reason='healthy=false'),),
        failing_stage=4,
        failing_cluster='az',
    )
    d = r.as_dict()
    assert d['verdict'] == 'FAIL'
    assert d['reason'] == 'stage 4 cluster=az: healthy=false'
    assert d['failing_stage'] == 4
    assert d['failing_cluster'] == 'az'
    assert len(d['stages']) == 1
    assert d['stages'][0]['reason'] == 'healthy=false'
