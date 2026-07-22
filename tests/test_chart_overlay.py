"""Unit tests for gate.tools.chart_overlay — pure parsing + composed verdict."""

from __future__ import annotations

import base64
import subprocess
import textwrap

import pytest

from gate.tools import chart_overlay
from gate.tools.chart_overlay import (
    CLUSTER_OVERLAY_REPOS,
    ChartFlipSignal,
    YamlDict,
    any_cluster_overlay_sets_flip,
    evidence_for_flip,
    fetch_overlay_yaml,
    find_overlay_pr_refs,
    parse_chart_flip_signals,
)

# ---------------------------------------------------------------------------
# Sample diffs
# ---------------------------------------------------------------------------

# Realistic newly-added toggle whose comment claims a per-cluster GitOps
# overlay flips it on (mirrors the shape of `externalSecrets.gcp.enabled`
# in charts/leartech-automated-agent/values.yaml).
FLIP_DIFF_WITH_OVERLAY_HINT = textwrap.dedent(
    """\
    diff --git a/charts/some-chart/values.yaml b/charts/some-chart/values.yaml
    index 1234567..89abcde 100644
    --- a/charts/some-chart/values.yaml
    +++ b/charts/some-chart/values.yaml
    @@ -10,4 +10,10 @@
     existing: block
    +# Postgres-backed catalog. Disabled by default; flip via
    +# configs/some-chart.yaml on clusters with CNPG.
    +postgresql:
    +  enabled: false  # set true on AZ via configs/some-chart.yaml
    +  clusterName: shared
    +  databaseName: some_chart
    """
)

# A toggle whose comment does NOT claim a prod overlay — should be ignored.
FLIP_DIFF_NO_HINT = textwrap.dedent(
    """\
    diff --git a/charts/some-chart/values.yaml b/charts/some-chart/values.yaml
    index 1234567..89abcde 100644
    --- a/charts/some-chart/values.yaml
    +++ b/charts/some-chart/values.yaml
    @@ -10,4 +10,6 @@
     existing: block
    +# Optional autoscaling. Turn on manually when the pod hits CPU ceiling.
    +autoscaling:
    +  enabled: false
    """
)

# A toggle in a NON-chart file — should be ignored.
FLIP_DIFF_NOT_CHART = textwrap.dedent(
    """\
    diff --git a/app/config.yaml b/app/config.yaml
    index 1234567..89abcde 100644
    --- a/app/config.yaml
    +++ b/app/config.yaml
    @@ -10,3 +10,5 @@
     other: value
    +# set true on AZ via configs/some-chart.yaml
    +feature:
    +  enabled: false
    """
)

# Two flips in one diff, one hinted and one not.
FLIP_DIFF_MIXED = textwrap.dedent(
    """\
    diff --git a/charts/some-chart/values.yaml b/charts/some-chart/values.yaml
    --- a/charts/some-chart/values.yaml
    +++ b/charts/some-chart/values.yaml
    @@ -1,4 +1,12 @@
     header: line
    +# just a comment explaining defaults
    +ingress:
    +  enabled: false
    +
    +# ExternalSecret rendering — flip via configs/some-chart.yaml to
    +# opt in production.
    +externalSecrets:
    +  enabled: false
    """
)


# ---------------------------------------------------------------------------
# parse_chart_flip_signals
# ---------------------------------------------------------------------------


def test_parse_signal_with_overlay_hint_comment() -> None:
    signals = parse_chart_flip_signals(FLIP_DIFF_WITH_OVERLAY_HINT)
    assert len(signals) == 1
    s = signals[0]
    assert s.chart_path == 'charts/some-chart/values.yaml'
    assert s.chart_name == 'some-chart'
    assert s.dotted_key == 'postgresql.enabled'
    assert s.default_value is False
    assert 'set true' in s.hint_snippet.lower()


def test_parse_ignores_toggle_without_overlay_hint() -> None:
    """A toggle default of false with no prod-overlay comment is NOT a signal."""
    signals = parse_chart_flip_signals(FLIP_DIFF_NO_HINT)
    assert signals == []


def test_parse_ignores_non_chart_paths() -> None:
    """Even a comment that would match the hint isn't a signal outside charts/*/values.yaml."""
    signals = parse_chart_flip_signals(FLIP_DIFF_NOT_CHART)
    assert signals == []


def test_parse_returns_only_hinted_flips_in_mixed_diff() -> None:
    signals = parse_chart_flip_signals(FLIP_DIFF_MIXED)
    assert [s.dotted_key for s in signals] == ['externalSecrets.enabled']
    assert 'flip via configs' in signals[0].hint_snippet.lower()


def test_parse_ignores_context_only_lines() -> None:
    """A context (unchanged) line matching `X.enabled: false` should NOT surface —
    only added lines (kind='+') count."""
    diff = textwrap.dedent(
        """\
        diff --git a/charts/x/values.yaml b/charts/x/values.yaml
        --- a/charts/x/values.yaml
        +++ b/charts/x/values.yaml
        @@ -10,3 +10,4 @@
         # set true on AZ via configs/x.yaml
         postgresql:
           enabled: false
        +new_unrelated: value
        """
    )
    assert parse_chart_flip_signals(diff) == []


def test_parse_ignores_non_enabled_boolean_toggles() -> None:
    """Non-`enabled` boolean keys are NOT chart flips — avoids explosion of false positives."""
    diff = textwrap.dedent(
        """\
        diff --git a/charts/x/values.yaml b/charts/x/values.yaml
        --- a/charts/x/values.yaml
        +++ b/charts/x/values.yaml
        @@ -1,4 +1,7 @@
         header: line
        +# set true on AZ via configs/x.yaml
        +postgresql:
        +  debug: false
        """
    )
    assert parse_chart_flip_signals(diff) == []


def test_parse_handles_multiple_hint_phrasings() -> None:
    """Several overlay-hint phrasings should all resolve to signals."""
    diff = textwrap.dedent(
        """\
        diff --git a/charts/x/values.yaml b/charts/x/values.yaml
        --- a/charts/x/values.yaml
        +++ b/charts/x/values.yaml
        @@ -1,20 +1,20 @@
         header: line
        +# Flipped on in production via GitOps overlay.
        +a:
        +  enabled: false
        +
        +# Opts in via prod overlay.
        +b:
        +  enabled: false
        +
        +# Enabled in production via GitOps.
        +c:
        +  enabled: false
        """
    )
    dotted = [s.dotted_key for s in parse_chart_flip_signals(diff)]
    assert dotted == ['a.enabled', 'b.enabled', 'c.enabled']


# ---------------------------------------------------------------------------
# find_overlay_pr_refs
# ---------------------------------------------------------------------------


def test_find_refs_matches_owner_repo_hash_form() -> None:
    body = 'See linked overlay PR mikelear/jx-build-cluster-gsm#42 for the flip.'
    assert find_overlay_pr_refs('', body) == ['mikelear/jx-build-cluster-gsm#42']


def test_find_refs_matches_full_github_url_and_normalises() -> None:
    body = 'Overlay: https://github.com/mikelear/jx-build-cluster-akv/pull/17'
    assert find_overlay_pr_refs(body) == ['mikelear/jx-build-cluster-akv#17']


def test_find_refs_dedupes_across_sources() -> None:
    title = 'feat(chart): plumb dcr flag (mikelear/jx-build-cluster-gsm#5)'
    body = (
        'See https://github.com/mikelear/jx-build-cluster-gsm/pull/5 for the overlay '
        'and mikelear/jx-build-cluster-gsm#5 for good measure.'
    )
    assert find_overlay_pr_refs(title, body) == ['mikelear/jx-build-cluster-gsm#5']


def test_find_refs_ignores_non_cluster_repo_refs() -> None:
    body = 'Consumer PR mikelear/leartech-automated-agent#61 mentions no overlay.'
    assert find_overlay_pr_refs(body) == []


def test_find_refs_empty_when_no_signal() -> None:
    assert find_overlay_pr_refs('title', 'body') == []
    assert find_overlay_pr_refs('', '') == []


# ---------------------------------------------------------------------------
# Overlay match — matches_overlay_value / any_cluster_overlay_sets_flip
# ---------------------------------------------------------------------------


def _make_signal(dotted_key: str = 'postgresql.enabled', chart_name: str = 'some-chart') -> ChartFlipSignal:
    return ChartFlipSignal(
        chart_path=f'charts/{chart_name}/values.yaml',
        chart_name=chart_name,
        dotted_key=dotted_key,
        default_value=False,
        hint_snippet='set true on AZ via configs/some-chart.yaml',
    )


def test_matches_overlay_value_true_when_key_overrides_default() -> None:
    sig = _make_signal('postgresql.enabled')
    assert sig.matches_overlay_value({'postgresql': {'enabled': True}}) is True


def test_matches_overlay_value_false_when_key_absent() -> None:
    sig = _make_signal('postgresql.enabled')
    assert sig.matches_overlay_value({'other': {'enabled': True}}) is False


def test_matches_overlay_value_false_when_value_matches_default() -> None:
    """Overlay YAML declaring the SAME default as the chart isn't an override."""
    sig = _make_signal('postgresql.enabled')  # default_value=False
    assert sig.matches_overlay_value({'postgresql': {'enabled': False}}) is False


def test_any_cluster_overlay_sets_flip_hits_first_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    sig = _make_signal('postgresql.enabled', chart_name='some-chart')

    calls: list[tuple[str, str]] = []

    def fake_fetch(cluster_repo: str, path: str, ref: str = 'main') -> YamlDict:
        calls.append((cluster_repo, path))
        if cluster_repo == CLUSTER_OVERLAY_REPOS['gcp']:
            return {'postgresql': {'enabled': True}}
        return {}

    monkeypatch.setattr(chart_overlay, 'fetch_overlay_yaml', fake_fetch)
    reason = any_cluster_overlay_sets_flip(sig, envs=('jx-staging',))
    assert 'jx-build-cluster-gsm' in reason
    assert 'postgresql.enabled' in reason
    assert calls[0] == (CLUSTER_OVERLAY_REPOS['gcp'], 'helmfiles/jx-staging/configs/some-chart.yaml')


def test_any_cluster_overlay_returns_empty_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    sig = _make_signal()
    monkeypatch.setattr(chart_overlay, 'fetch_overlay_yaml', lambda *a, **kw: {})
    assert any_cluster_overlay_sets_flip(sig) == ''


# ---------------------------------------------------------------------------
# Composed verdict
# ---------------------------------------------------------------------------


def test_evidence_for_flip_ok_when_overlay_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    sig = _make_signal()
    monkeypatch.setattr(
        chart_overlay,
        'fetch_overlay_yaml',
        lambda *a, **kw: {'postgresql': {'enabled': True}},
    )
    ok, reason = evidence_for_flip(sig, pr_title='irrelevant', pr_body='irrelevant')
    assert ok
    assert 'overlay already sets' in reason


def test_evidence_for_flip_ok_when_pr_links_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    sig = _make_signal()
    monkeypatch.setattr(chart_overlay, 'fetch_overlay_yaml', lambda *a, **kw: {})
    ok, reason = evidence_for_flip(
        sig,
        pr_title='feat(chart): add dcr toggle',
        pr_body='Linked overlay PR: mikelear/jx-build-cluster-gsm#77',
    )
    assert ok
    assert 'jx-build-cluster-gsm#77' in reason


def test_evidence_for_flip_fails_when_no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #61-style regression — chart flip added, no overlay, no linked PR."""
    sig = _make_signal()
    monkeypatch.setattr(chart_overlay, 'fetch_overlay_yaml', lambda *a, **kw: {})
    ok, reason = evidence_for_flip(sig, pr_title='shipped it', pr_body='trust me it works')
    assert not ok
    assert 'no overlay YAML' in reason
    assert 'postgresql.enabled' in reason
    assert 'set true on AZ' in reason  # hint_snippet surfaced in the failure text


# ---------------------------------------------------------------------------
# fetch_overlay_yaml — every error branch must fold into {} (no exceptions)
# ---------------------------------------------------------------------------


def test_fetch_overlay_yaml_returns_empty_when_gh_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero ``gh`` exit (RuntimeError from ``_gh``) → empty dict, no raise."""

    def raising_gh(args: list[str]) -> str:
        raise RuntimeError('gh api ...: 404 Not Found')

    monkeypatch.setattr(chart_overlay, '_gh', raising_gh)
    assert fetch_overlay_yaml('mikelear/foo', 'helmfiles/jx-staging/configs/bar.yaml') == {}


def test_fetch_overlay_yaml_returns_empty_when_gh_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess timeout inside ``_gh`` bubbles up as RuntimeError → empty dict."""

    def timing_out(args: list[str]) -> str:
        raise RuntimeError('gh api ... timed out after 30s')

    monkeypatch.setattr(chart_overlay, '_gh', timing_out)
    assert fetch_overlay_yaml('mikelear/foo', 'path') == {}


def test_fetch_overlay_yaml_returns_empty_on_blank_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh`` succeeds but returns whitespace-only content → empty dict."""
    monkeypatch.setattr(chart_overlay, '_gh', lambda args: '   \n')
    assert fetch_overlay_yaml('mikelear/foo', 'path') == {}


def test_fetch_overlay_yaml_returns_empty_on_invalid_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload isn't valid base64 → ValueError/binascii.Error caught → empty dict."""
    # A non-base64 character sequence (``!`` isn't in the base64 alphabet under strict mode;
    # for safety we pick a length not a multiple of 4).
    monkeypatch.setattr(chart_overlay, '_gh', lambda args: '!!!not-base64!!!')
    assert fetch_overlay_yaml('mikelear/foo', 'path') == {}


def test_fetch_overlay_yaml_returns_empty_on_malformed_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Base64 decodes fine but content isn't valid YAML → YAMLError caught → empty dict."""
    payload = base64.b64encode(b'foo: [unclosed').decode()
    monkeypatch.setattr(chart_overlay, '_gh', lambda args: payload)
    assert fetch_overlay_yaml('mikelear/foo', 'path') == {}


def test_fetch_overlay_yaml_returns_empty_on_non_mapping_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """YAML parses to a list / scalar / null instead of a mapping → empty dict."""
    for body in (b'- item1\n- item2\n', b'"just a scalar"\n', b'null\n'):
        payload = base64.b64encode(body).decode()
        monkeypatch.setattr(chart_overlay, '_gh', lambda args, _p=payload: _p)
        assert fetch_overlay_yaml('mikelear/foo', 'path') == {}


def test_fetch_overlay_yaml_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one successful path — valid base64 → valid YAML mapping → parsed dict."""
    payload = base64.b64encode(b'postgresql:\n  enabled: true\n').decode()
    monkeypatch.setattr(chart_overlay, '_gh', lambda args: payload)
    assert fetch_overlay_yaml('mikelear/foo', 'path') == {'postgresql': {'enabled': True}}


# ---------------------------------------------------------------------------
# _gh — subprocess wrapper's error paths
# ---------------------------------------------------------------------------


def test_gh_raises_runtime_error_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit → RuntimeError with the stderr trailer."""

    class FakeCompleted:
        returncode = 1
        stdout = ''
        stderr = '404 Not Found\n'

    def fake_run(cmd: list[str], **kwargs: object) -> FakeCompleted:
        return FakeCompleted()

    monkeypatch.setattr(chart_overlay.subprocess, 'run', fake_run)
    with pytest.raises(RuntimeError, match='404 Not Found'):
        chart_overlay._gh(['api', 'anything'])


def test_gh_raises_runtime_error_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``subprocess.TimeoutExpired`` is translated to RuntimeError so callers
    can fold both errors into the empty-overlay fallback uniformly."""

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(chart_overlay.subprocess, 'run', fake_run)
    with pytest.raises(RuntimeError, match='timed out'):
        chart_overlay._gh(['api', 'anything'])
