"""Tests for the v6p0.6 step-2 dispatcher extensions.

The watcher's heuristic dispatcher (`gate.agent.step_failure_diagnosis`)
recognises ~13 canonical failure shapes today. Before v6p0.6, govulncheck
advisories, dynamic-scan SARIF findings, and Helm preview-deploy errors
all fell through to `escalate`. Step 2 splits them into actionable
subclasses BEFORE the generic catch-alls fire.

This module exercises:

1. **govulncheck** — text-pattern recognition routes to ``fix_code``
   with the advisory ID captured in the log_tail.
2. **dynamic-scan severity split** — HIGH/CRITICAL → ``fix_code``,
   LOW/INFORMATIONAL → ``escalate``.
3. **Helm preview-deploy subclasses** — missing value → ``fix_code``,
   missing secret → ``escalate``, timeout → ``retry``.
4. **Existing dispatch unchanged** — the 13 original shapes still
   classify as they did before; precedence ordering is preserved.

These are deliberately separate from ``test_step_failure_diagnosis``
so that the diff for the v6p0.6 work stands alone and is easy to
review. The new test file is also where future heuristic extensions
should land — keeping the original test file as the "v1 canon" and the
extended file as the "specific-shapes" companion.
"""

from __future__ import annotations

import pytest

from gate.agent.step_failure_diagnosis import (
    ACTION_ESCALATE,
    ACTION_FIX_CODE,
    ACTION_FIX_TEST,
    ACTION_REBASE,
    ACTION_RETRY,
    CLASSIFICATIONS,
    classify_step_failure,
)

# ─── govulncheck recognition ─────────────────────────────────────────────────


def test_govulncheck_text_advisory_routes_to_fix_code() -> None:
    """A canonical `govulncheck` text-mode log dispatches to ``fix_code``.

    Sample shape taken from a real govulncheck advisory: the tool prints
    one or more ``Vulnerability #N: GO-YYYY-NNNN`` blocks followed by
    advice on which module to bump. The agent's response is a module
    bump (renovate-style upgrade), which is a ``fix_code`` action.
    """
    log = (
        '=== Symbol Results ===\n'
        '\n'
        'Vulnerability #1: GO-2024-3107\n'
        '    Decoder.Decode in archive/zip can panic on malformed input.\n'
        '  More info: https://pkg.go.dev/vuln/GO-2024-3107\n'
        '  Module: archive/zip\n'
        '    Found in: stdlib@go1.22.4\n'
        '    Fixed in: stdlib@go1.22.5\n'
        '  Your code is affected by 1 vulnerability from the Go standard library.\n'
    )
    failure = classify_step_failure('govulncheck', log, pipelinerun='run-abc')
    assert failure.classification == 'govulncheck_vulnerability'
    assert failure.action == ACTION_FIX_CODE
    # The advisory ID stays in log_tail for the dispatcher prompt — the
    # agent's enrichment uses it verbatim when constructing the fix context.
    assert 'GO-2024-3107' in failure.log_tail


def test_govulncheck_id_alone_is_sufficient() -> None:
    """Even without the textual preamble, a bare GO-YYYY-NNNN advisory
    ID triggers the regex and routes to fix_code. Some govulncheck
    invocations (e.g. when output is heavily truncated by Tekton) only
    surface the ID line."""
    log = '    GO-2025-0042\n'
    failure = classify_step_failure('govulncheck', log)
    assert failure.classification == 'govulncheck_vulnerability'
    assert failure.action == ACTION_FIX_CODE


def test_govulncheck_alternate_step_names() -> None:
    """`govulncheck`, `vulncheck`, `go-vuln` all match — different
    leartech repos name the step slightly differently."""
    log = 'Vulnerability #1: GO-2024-1234\nYour code is affected by 1 vulnerability'
    for step_name in ('govulncheck', 'vulncheck', 'go-vulncheck', 'lint-govulncheck'):
        failure = classify_step_failure(step_name, log)
        assert failure.classification == 'govulncheck_vulnerability', f'step_name={step_name!r} did not match'


# ─── dynamic-scan severity split ─────────────────────────────────────────────


def test_dynamic_scan_high_sarif_routes_to_fix_code() -> None:
    """A SARIF-shaped high-severity finding dispatches to ``fix_code``.

    The SARIF JSON is collapsed into the step log when the gate runs in
    text mode; the dispatcher recognises the canonical ``"level": "error"``
    token. (Step 1's structured parser reads the JSON proper; this
    text-pattern path is the fallback when only the log is available.)
    """
    log = (
        '+ /scanner/run-zap-baseline\n'
        '{\n'
        '  "runs": [{\n'
        '    "results": [{\n'
        '      "ruleId": "10038", "level": "error",\n'
        '      "message": {"text": "CSP header not set"},\n'
        '      "properties": {"severity": "high"}\n'
        '    }]\n'
        '  }]\n'
        '}\n'
    )
    failure = classify_step_failure('gcp/dynamic-scan', log)
    assert failure.classification == 'dynamic_scan_high_finding'
    assert failure.action == ACTION_FIX_CODE


def test_dynamic_scan_high_zap_textual_routes_to_fix_code() -> None:
    """ZAP's textual summary line — "High (Medium): ..." — also routes
    to fix_code. The bracketed second token is ZAP's confidence; the
    severity is the leading word."""
    log = (
        'ZAP Scanning Report\n'
        'High (Medium): Cross-Site Scripting (Reflected) [40012]\n'
        '  Description: Cross-site Scripting (XSS) is an attack technique...\n'
    )
    failure = classify_step_failure('dynamic-scan', log)
    assert failure.classification == 'dynamic_scan_high_finding'
    assert failure.action == ACTION_FIX_CODE


def test_dynamic_scan_low_sarif_routes_to_escalate() -> None:
    """A LOW/INFORMATIONAL SARIF finding is noise — the dispatcher
    routes it to escalate so the agent doesn't churn on chasing it."""
    log = (
        '{\n'
        '  "runs": [{\n'
        '    "results": [{\n'
        '      "ruleId": "10027", "level": "note",\n'
        '      "message": {"text": "Information Disclosure - Suspicious Comments"},\n'
        '      "properties": {"severity": "low"}\n'
        '    }]\n'
        '  }]\n'
        '}\n'
    )
    failure = classify_step_failure('az/dynamic-scan', log)
    assert failure.classification == 'dynamic_scan_low_finding'
    assert failure.action == ACTION_ESCALATE


def test_dynamic_scan_low_textual_routes_to_escalate() -> None:
    log = 'Low (Medium): X-Content-Type-Options Header Missing [10021]\n'
    failure = classify_step_failure('dynamic-scan', log)
    assert failure.classification == 'dynamic_scan_low_finding'
    assert failure.action == ACTION_ESCALATE


def test_dynamic_scan_high_wins_when_both_severities_present() -> None:
    """If a scan finds BOTH high and low findings (common in practice),
    the high heuristic fires first because it's higher up in the matrix —
    that's correct: fix the high one, the low ones come along for free."""
    log = (
        'High (Medium): Cross-Site Scripting (Reflected) [40012]\n'
        'Low (Medium): X-Content-Type-Options Header Missing [10021]\n'
    )
    failure = classify_step_failure('dynamic-scan', log)
    assert failure.classification == 'dynamic_scan_high_finding'
    assert failure.action == ACTION_FIX_CODE


# ─── Helm preview-deploy subclasses ──────────────────────────────────────────


def test_helm_missing_value_routes_to_fix_code() -> None:
    """A Helm INSTALLATION FAILED whose root cause is a missing chart
    value (nil pointer / map has no entry) is a chart-level fix the
    agent can attempt — patch ``values.yaml`` or the template."""
    log = (
        'helm upgrade --install preview-foo .lighthouse/jenkins-x/charts/preview\n'
        'Error: INSTALLATION FAILED: template: foo/templates/deployment.yaml:18:24:\n'
        '  executing "foo/templates/deployment.yaml" at <.Values.image.repository>:\n'
        '  nil pointer evaluating interface {}.repository\n'
    )
    failure = classify_step_failure('helm-promote', log)
    assert failure.classification == 'helm_missing_value'
    assert failure.action == ACTION_FIX_CODE


def test_helm_missing_required_key_routes_to_fix_code() -> None:
    log = (
        'Error: UPGRADE FAILED: execution error at\n'
        '  (foo/templates/configmap.yaml:24:13): missing required key: env.DATABASE_URL\n'
    )
    failure = classify_step_failure('preview', log)
    assert failure.classification == 'helm_missing_value'
    assert failure.action == ACTION_FIX_CODE


def test_helm_missing_secret_routes_to_escalate() -> None:
    """A missing-secret deploy failure is operator territory — the
    Secret has to be seeded into the preview namespace by hand (or by
    sealed-secrets), and the agent doesn't have those credentials. So
    we escalate rather than try to fake it."""
    log = (
        'Error: INSTALLATION FAILED: failed pre-install: 1 error occurred:\n'
        '  * timed out waiting for resource creation: secrets "preview-db-creds" not found\n'
    )
    failure = classify_step_failure('helm-promote', log)
    assert failure.classification == 'helm_missing_secret'
    assert failure.action == ACTION_ESCALATE


def test_helm_missing_secret_volume_mount_routes_to_escalate() -> None:
    """A secret referenced by a volume mount that doesn't exist surfaces
    via the kubelet's MountVolume.SetUp message — same root cause, same
    classification."""
    log = (
        'Warning  FailedMount  pod/preview-foo-0  MountVolume.SetUp failed for volume "creds":\n'
        '  secret "preview-foo-creds" not found\n'
    )
    failure = classify_step_failure('preview-deploy', log)
    assert failure.classification == 'helm_missing_secret'
    assert failure.action == ACTION_ESCALATE


def test_helm_timeout_routes_to_retry() -> None:
    """A Helm rollout that timed out waiting for the resource to become
    ready is most often a transient race (slow image pull, init
    container slow to drain) — retest rather than retest-as-code-fix."""
    log = (
        'helm upgrade --install preview-foo ./charts/preview --wait --timeout 10m\n'
        'Error: UPGRADE FAILED: timed out waiting for the condition\n'
    )
    failure = classify_step_failure('helm-promote', log)
    assert failure.classification == 'helm_timeout'
    assert failure.action == ACTION_RETRY


def test_helm_generic_failure_still_routes_to_preview_deploy_failure() -> None:
    """A Helm failure that doesn't match the missing-value / missing-secret /
    timeout subclasses falls through to the generic ``preview_deploy_failure``
    (escalate), as it did pre-v6p0.6."""
    log = (
        'helm upgrade --install preview-foo ./charts/preview\n'
        'Error: INSTALLATION FAILED: release failed: no matches for kind "FlinkDeployment"\n'
    )
    failure = classify_step_failure('helm-promote', log)
    assert failure.classification == 'preview_deploy_failure'
    assert failure.action == ACTION_ESCALATE


# ─── Precedence: subclasses BEFORE generics ──────────────────────────────────


def test_govulncheck_wins_over_security_scan_finding() -> None:
    """A govulncheck advisory log that also incidentally contains
    ``CVE-`` (e.g. cross-referencing the CVE behind the GO advisory)
    must still classify as ``govulncheck_vulnerability`` so the action
    is fix_code rather than escalate."""
    log = 'Vulnerability #1: GO-2024-3107\n  See also: CVE-2024-24789\n  Your code is affected by 1 vulnerability.\n'
    failure = classify_step_failure('govulncheck', log)
    assert failure.classification == 'govulncheck_vulnerability'
    assert failure.action == ACTION_FIX_CODE


def test_dynamic_scan_high_wins_over_security_scan_finding() -> None:
    """A dynamic-scan log with both a SARIF level token and a Trivy-style
    HIGH summary should route to ``dynamic_scan_high_finding`` (fix_code)
    not ``security_scan_finding`` (escalate)."""
    log = '"level": "error"\nTotal: 3 vulnerabilities found\nHIGH: 3\n'
    failure = classify_step_failure('dynamic-scan', log)
    assert failure.classification == 'dynamic_scan_high_finding'
    assert failure.action == ACTION_FIX_CODE


def test_helm_missing_value_wins_over_preview_deploy_failure() -> None:
    """A Helm INSTALLATION FAILED whose log also contains the nil-pointer
    template error must route to ``helm_missing_value`` not the generic
    ``preview_deploy_failure``."""
    log = (
        'Error: INSTALLATION FAILED: template error\n'
        '  nil pointer evaluating interface {}.image at <.Values.image.tag>\n'
        '  release "preview-foo" failed\n'
    )
    failure = classify_step_failure('helm-promote', log)
    assert failure.classification == 'helm_missing_value'
    assert failure.action == ACTION_FIX_CODE


# ─── Backward-compat: existing dispatchers still work ────────────────────────


@pytest.mark.parametrize(
    ('step_name', 'log_tail', 'expected_classification', 'expected_action'),
    [
        # ruff (lint) — unchanged
        (
            'ruff',
            'gate/foo.py:1:1: E501 line too long (130 > 120)\nFound 1 error.',
            'ruff_lint_error',
            ACTION_FIX_CODE,
        ),
        # mypy — unchanged
        (
            'mypy',
            'gate/foo.py:14: error: Incompatible types\nFound 1 error in 1 file',
            'mypy_type_error',
            ACTION_FIX_CODE,
        ),
        # pytest — unchanged
        (
            'pytest',
            '= FAILURES =\nAssertionError\n= short test summary info =\nFAILED tests/test_foo.py',
            'pytest_test_failure',
            ACTION_FIX_TEST,
        ),
        # kaniko — unchanged
        (
            'build-image',
            'error building image: executor failed running [/bin/sh -c uv sync]\nCOPY failed',
            'kaniko_build_failure',
            ACTION_ESCALATE,
        ),
        # git merge conflict — unchanged
        (
            'git-clone',
            'CONFLICT (content): Merge conflict in app/main.py\nAutomatic merge failed',
            'git_merge_conflict',
            ACTION_REBASE,
        ),
        # OOM — unchanged, still beats step-specific patterns
        (
            'pytest',
            '= FAILURES =\nOOMKilled\nexit code 137',
            'tekton_step_oom',
            ACTION_ESCALATE,
        ),
    ],
)
def test_existing_dispatchers_unchanged(
    step_name: str,
    log_tail: str,
    expected_classification: str,
    expected_action: str,
) -> None:
    """The v6p0.6 additions must not regress any of the original
    13 canonical shapes. This parametrised test pins the existing
    behaviour as a guard against accidental ordering changes."""
    failure = classify_step_failure(step_name, log_tail)
    assert failure.classification == expected_classification
    assert failure.action == expected_action


# ─── Classification table integrity ──────────────────────────────────────────


def test_classification_table_includes_v6p0_6_additions() -> None:
    """The CLASSIFICATIONS map is the source of truth for which actions
    are dispatched for each classification. The v6p0.6 additions must
    be wired into the map (else ``classify_step_failure`` would
    KeyError on the new shapes)."""
    additions = {
        'govulncheck_vulnerability': ACTION_FIX_CODE,
        'dynamic_scan_high_finding': ACTION_FIX_CODE,
        'dynamic_scan_low_finding': ACTION_ESCALATE,
        'helm_missing_value': ACTION_FIX_CODE,
        'helm_missing_secret': ACTION_ESCALATE,
        'helm_timeout': ACTION_RETRY,
    }
    for classification, expected_action in additions.items():
        assert classification in CLASSIFICATIONS, f'{classification!r} missing from CLASSIFICATIONS map'
        assert CLASSIFICATIONS[classification] == expected_action, (
            f'{classification!r} maps to {CLASSIFICATIONS[classification]!r}, expected {expected_action!r}'
        )
