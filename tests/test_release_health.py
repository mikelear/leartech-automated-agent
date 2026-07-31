"""Determinism pins for the scripted /health probe.

These tests are the whole point of the fix: the same setup MUST produce the same
verdict every time. Historically the LLM improvised — one run curled the ingress
/health and PASSED, another concluded "kubectl not available" and FAILED closed.
The verdict now comes from Python responding to HTTP status codes, on injected
``now``/``sleep``/``client`` so the schedule is fully pinned.

Key invariants:

* endpoint returning 200 → PASS with NO kubectl (this is the fix's headline);
* endpoint returning 502 then 200 → retries then PASSES (transient during
  jx-boot reconcile);
* endpoint returning persistent non-200 → FAILS with a clear reason;
* verdict does NOT depend on kubectl (the module simply never touches it).
"""

from __future__ import annotations

import sys

import httpx
import pytest

from gate.agent import release_health
from gate.agent.release_health import (
    HostProbe,
    ProbeResult,
    parse_health_targets,
    probe_health_targets,
    resolve_targets_from_inputs,
)

# ── virtual clock / sleep so tests never touch real time ──────────────────────


class _FakeClock:
    """Deterministic time source paired with a sleep that advances it."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += secs


def _mock_client(responses_by_url: dict[str, list[object]]) -> httpx.Client:
    """Build an httpx.Client whose ``get`` returns each URL's next queued response.

    A queued item may be an ``int`` (interpreted as HTTP status) or an
    ``Exception`` instance (raised as if the transport failed).
    """
    remaining = {url: list(queue) for url, queue in responses_by_url.items()}

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in remaining or not remaining[url]:
            raise AssertionError(f'unexpected request to {url}')
        nxt = remaining[url].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(int(nxt))

    return httpx.Client(transport=httpx.MockTransport(_handler))


# ── the determinism headline ─────────────────────────────────────────────────


def test_probe_passes_on_200_with_no_kubectl_involved() -> None:
    """The core fix: 200 → PASS, verdict computed WITHOUT any kubectl involvement."""
    clock = _FakeClock()
    client = _mock_client({'https://hello-go.example.com/health': [200]})
    with client:
        result = probe_health_targets(
            ['https://hello-go.example.com/health'],
            budget_seconds=60.0,
            backoff_seconds=5.0,
            now=clock.now,
            sleep=clock.sleep,
            client=client,
        )
    assert result.verdict == 'PASS'
    assert result.reason is None
    assert [p.ok for p in result.probes] == [True]
    assert [p.last_status for p in result.probes] == [200]
    # No sleep needed when the first probe returns 200 — the schedule is pinned.
    assert clock.sleeps == []


def test_probe_module_does_not_shell_out_or_touch_kubernetes() -> None:
    """kubectl is deliberately NOT touched — its absence is not-a-fail (asymmetry closed).

    Enforced by CODE, not docstring — the module explains why it doesn't touch kubectl,
    which is fine. What we forbid is actually running it: no ``subprocess`` import, no
    kubernetes SDK import.
    """
    import gate.agent.release_health as mod  # noqa: PLC0415 — assertion-scoped

    # No subprocess-based shell-outs (that's how a caller would run kubectl).
    assert not hasattr(mod, 'subprocess')
    # No kubernetes SDK either. The module namespace pulls in only httpx-shaped bits.
    for attr in ('kubernetes', 'client', 'ApiClient', 'CoreV1Api'):
        assert not hasattr(mod, attr) or attr in {'client'}  # httpx.Client alias would be fine
    # And the module source doesn't shell out (no subprocess/os.system/popen).
    src = (release_health.__file__ or '').replace('.pyc', '.py')
    with open(src) as fh:
        text = fh.read()
    for forbidden in ('subprocess', 'os.system', 'popen(', 'kubernetes.client'):
        assert forbidden not in text, f'release_health should not use {forbidden!r}'
    # Sanity: sys is imported here for module-listing only; keep the lint tidy.
    _ = sys


# ── transient recovery: 502 then 200 → PASS ──────────────────────────────────


@pytest.mark.parametrize('transient_status', [502, 503, 504])
def test_probe_retries_transient_status_then_passes_on_200(transient_status: int) -> None:
    """502/503/504 during jx-boot reconcile → keep retrying → 200 → PASS."""
    clock = _FakeClock()
    url = 'https://hello-go.example.com/health'
    client = _mock_client({url: [transient_status, 200]})
    with client:
        result = probe_health_targets(
            [url],
            budget_seconds=120.0,
            backoff_seconds=5.0,
            now=clock.now,
            sleep=clock.sleep,
            client=client,
        )
    assert result.verdict == 'PASS'
    p = result.probes[0]
    assert p.ok and p.last_status == 200
    assert p.attempts == 2
    assert clock.sleeps == [5.0]  # one backoff between the two attempts


def test_probe_retries_transport_error_then_passes_on_200() -> None:
    """Transport errors (LB not answering yet) are treated like transient 5xx — retried."""
    clock = _FakeClock()
    url = 'https://hello-go.example.com/health'
    conn_err = httpx.ConnectError('connection refused')
    client = _mock_client({url: [conn_err, 200]})
    with client:
        result = probe_health_targets(
            [url],
            budget_seconds=120.0,
            backoff_seconds=3.0,
            now=clock.now,
            sleep=clock.sleep,
            client=client,
        )
    assert result.verdict == 'PASS'
    assert result.probes[0].attempts == 2


# ── persistent non-200 → FAIL with clear reason ──────────────────────────────


def test_probe_fails_on_persistent_transient_within_budget() -> None:
    """502 forever, budget expires → FAIL naming the last status + attempts."""
    clock = _FakeClock()
    url = 'https://hello-go.example.com/health'
    # Enough 502s to exhaust the budget (10s budget / 3s backoff → at most 4 tries).
    client = _mock_client({url: [502] * 10})
    with client:
        result = probe_health_targets(
            [url],
            budget_seconds=10.0,
            backoff_seconds=3.0,
            now=clock.now,
            sleep=clock.sleep,
            client=client,
        )
    assert result.verdict == 'FAIL'
    assert result.reason is not None
    assert 'no 200' in result.reason
    assert '502' in result.reason
    assert result.probes[0].attempts >= 2


@pytest.mark.parametrize('hard_status', [404, 400, 500, 501])
def test_probe_fails_immediately_on_non_transient_non_200(hard_status: int) -> None:
    """404/500 etc. are NOT retried — retrying can't fix a missing route or a crashed app."""
    clock = _FakeClock()
    url = 'https://hello-go.example.com/health'
    client = _mock_client({url: [hard_status]})
    with client:
        result = probe_health_targets(
            [url],
            budget_seconds=60.0,
            backoff_seconds=5.0,
            now=clock.now,
            sleep=clock.sleep,
            client=client,
        )
    assert result.verdict == 'FAIL'
    assert result.probes[0].attempts == 1
    assert clock.sleeps == []  # no backoff wasted on a hard fail
    assert result.reason is not None
    assert str(hard_status) in result.reason
    assert 'non-transient' in result.reason


# ── multi-target: PASS iff every host returns 200 ────────────────────────────


def test_probe_requires_all_targets_pass() -> None:
    """Both clusters must return 200 for the verdict to be PASS — the passing run probed both."""
    clock = _FakeClock()
    gcp = 'https://hello-go-gcp.example.com/health'
    az = 'https://hello-go-az.example.com/health'
    client = _mock_client({gcp: [200], az: [500]})
    with client:
        result = probe_health_targets(
            [gcp, az],
            budget_seconds=30.0,
            backoff_seconds=5.0,
            now=clock.now,
            sleep=clock.sleep,
            client=client,
        )
    assert result.verdict == 'FAIL'
    assert result.reason is not None
    assert 'HTTP 500' in result.reason  # names the specific failing target


def test_probe_passes_both_targets_return_200() -> None:
    clock = _FakeClock()
    gcp = 'https://hello-go-gcp.example.com/health'
    az = 'https://hello-go-az.example.com/health'
    client = _mock_client({gcp: [200], az: [200]})
    with client:
        result = probe_health_targets(
            [gcp, az],
            budget_seconds=30.0,
            backoff_seconds=5.0,
            now=clock.now,
            sleep=clock.sleep,
            client=client,
        )
    assert result.verdict == 'PASS'


def test_empty_targets_is_deterministic_fail() -> None:
    """No targets → FAIL with a specific reason (no PASS-by-silence)."""
    result = probe_health_targets([], budget_seconds=1.0)
    assert result.verdict == 'FAIL'
    assert result.reason is not None
    assert 'no health targets' in result.reason


# ── target resolution: inputs override discovery ─────────────────────────────


def test_resolve_targets_from_inputs_prefers_healthurl() -> None:
    """healthUrl wins over host wins over transcript (most-deterministic first)."""
    targets = resolve_targets_from_inputs(
        {'healthUrl': 'https://custom.example/live'},
        'HEALTH_TARGETS: https://ignored.example/health',
    )
    assert targets == ['https://custom.example/live']


def test_resolve_targets_from_inputs_composes_host_and_healthpath() -> None:
    targets = resolve_targets_from_inputs(
        {'host': 'hello-go.example.com', 'healthPath': '/healthz'},
        '',
    )
    assert targets == ['https://hello-go.example.com/healthz']


def test_resolve_targets_from_inputs_accepts_list_of_hosts() -> None:
    targets = resolve_targets_from_inputs(
        {'host': ['gcp.example', 'az.example']},
        '',
    )
    assert targets == ['https://gcp.example/health', 'https://az.example/health']


def test_resolve_targets_from_inputs_accepts_comma_separated_hosts() -> None:
    targets = resolve_targets_from_inputs(
        {'host': 'gcp.example, az.example'},
        '',
    )
    assert targets == ['https://gcp.example/health', 'https://az.example/health']


def test_resolve_targets_defaults_healthpath_to_health() -> None:
    targets = resolve_targets_from_inputs({'host': 'foo.example'}, '')
    assert targets == ['https://foo.example/health']


def test_resolve_targets_prepends_slash_to_healthpath_if_missing() -> None:
    targets = resolve_targets_from_inputs({'host': 'foo.example', 'healthPath': 'livez'}, '')
    assert targets == ['https://foo.example/livez']


def test_resolve_targets_falls_back_to_transcript() -> None:
    targets = resolve_targets_from_inputs(
        {},
        'ok, targets:\nHEALTH_TARGETS: https://a.example/health, https://b.example/health\n',
    )
    assert targets == ['https://a.example/health', 'https://b.example/health']


def test_parse_health_targets_takes_last_line() -> None:
    """The LLM may narrate; the LAST HEALTH_TARGETS line wins."""
    text = (
        'HEALTH_TARGETS: https://old.example/health\n'
        'wait I need to retry\n'
        'HEALTH_TARGETS: https://new-a.example/health, https://new-b.example/health'
    )
    assert parse_health_targets(text) == [
        'https://new-a.example/health',
        'https://new-b.example/health',
    ]


def test_parse_health_targets_returns_empty_when_absent() -> None:
    assert parse_health_targets('nothing to see here') == []


# ── dataclass shapes ─────────────────────────────────────────────────────────


def test_host_probe_as_dict_matches_schema() -> None:
    p = HostProbe(url='https://x/health', ok=True, last_status=200, attempts=1, elapsed_seconds=0.42)
    d = p.as_dict()
    assert d == {
        'url': 'https://x/health',
        'ok': True,
        'last_status': 200,
        'attempts': 1,
        'elapsed_seconds': 0.42,
        'reason': None,
    }


def test_probe_result_as_dict_matches_schema() -> None:
    r = ProbeResult(
        verdict='FAIL',
        reason='HTTP 500 from …/health',
        probes=(
            HostProbe(
                url='https://x/health',
                ok=False,
                last_status=500,
                attempts=1,
                elapsed_seconds=0.1,
                reason='HTTP 500 from …/health',
            ),
        ),
    )
    d = r.as_dict()
    assert d['verdict'] == 'FAIL'
    assert d['reason'] == 'HTTP 500 from …/health'
    assert len(d['probes']) == 1
    assert d['probes'][0]['last_status'] == 500
