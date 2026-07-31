"""Deterministic /health probe for the infra agent's release-health-check action.

Root cause this closes (verified live 2026-07-30): the infra agent has no
kubectl (SA=default, agent-py image), so the historical stage-4
"confirm Deployment replicas + /health 200" relied on the LLM to IMPROVISE how
to verify. One run curled the ingress /health and PASSED; another concluded
"kubectl not available" and FAILED closed. Same setup, opposite verdicts.

This module CODIFIES the probe so the verdict is a function of what the
endpoints returned within the budget, not model whim. It:

* takes a fixed list of targets (URLs) discovered upstream (from an explicit
  ``healthUrl``/``host`` input, or by the LLM reading the merged GitOps YAML —
  discovery is separate from decision);
* HTTP GETs each target with retry + backoff within a caller-supplied budget;
* treats 502/503/504 and transport errors as **transient** (jx-boot is
  reconciling / LB is warming) and keeps retrying until the budget expires;
* treats any other non-200 as an **immediate** hard failure (deterministic —
  no "maybe the app is coming up", the app returned 404/500);
* verdicts PASS iff **every** required target returned 200 within the budget;
* is provider-agnostic Python — no ``claude_agent_sdk`` / ``anthropic`` imports,
  no shell-outs, no kubectl. Runs the same on Anthropic, DeepSeek, or a laptop.

kubectl is deliberately NOT touched: its absence must not cause a FAIL (this
lesson comes from the observed asymmetry). If a future step wants a
corroborating ``kubectl rollout status`` signal, it must live in a separate
optional helper and can only DOWNGRADE a PASS to WARN — never turn PASS into
FAIL.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

# HTTP statuses we treat as transient (jx-boot reconciling, ingress LB warming).
# Anything outside this set that isn't 200 is an immediate hard failure — a 404
# means the app didn't wire /health, a 500 means it crashed; retrying won't fix
# either. This is deliberately fixed, not a config: the point of the module is
# that the verdict is a function of what the endpoint returns, so the transient
# set is part of the spec.
TRANSIENT_STATUSES: frozenset[int] = frozenset({502, 503, 504})

# Machine-readable line the LLM emits after stages 1-3 pass; the LAST match
# wins (the model may narrate before its final line). Format:
#     HEALTH_TARGETS: https://host1.example/health, https://host2.example/health
_HEALTH_TARGETS_RE = re.compile(r'^\s*HEALTH_TARGETS:\s*(.+)$', re.MULTILINE)


@dataclass(frozen=True)
class HostProbe:
    """Outcome of probing ONE host — kept structured so the verdict reason
    stays specific ("HTTP 404 from https://…/health after 1 attempt in 0.2s"),
    which is what the infra-remediation loop needs to decide next actions.
    """

    url: str
    ok: bool
    last_status: int | None
    attempts: int
    elapsed_seconds: float
    reason: str | None = None  # populated on failure; None on PASS

    def as_dict(self) -> dict[str, Any]:
        """Structured logging shape."""
        return {
            'url': self.url,
            'ok': self.ok,
            'last_status': self.last_status,
            'attempts': self.attempts,
            'elapsed_seconds': round(self.elapsed_seconds, 3),
            'reason': self.reason,
        }


@dataclass(frozen=True)
class ProbeResult:
    """Rolled-up verdict over every target — PASS iff every probe is ok."""

    verdict: str  # 'PASS' | 'FAIL'
    reason: str | None  # None on PASS; concatenated failure reasons otherwise
    probes: tuple[HostProbe, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            'verdict': self.verdict,
            'reason': self.reason,
            'probes': [p.as_dict() for p in self.probes],
        }


def parse_health_targets(transcript: str) -> list[str]:
    """Extract targets from the LAST ``HEALTH_TARGETS: url[, url ...]`` line.

    The LLM emits this after stages 1-3 pass. We take the LAST occurrence so a
    retry/narration earlier in the transcript doesn't win over the final list.
    URLs are trimmed; empty entries are dropped. An unrecognised or missing
    line yields an empty list — the caller treats that as "no targets" and
    FAILs (deterministic — no targets, no PASS).
    """
    matches = _HEALTH_TARGETS_RE.findall(transcript)
    if not matches:
        return []
    return [u.strip() for u in matches[-1].split(',') if u.strip()]


def resolve_targets_from_inputs(inputs: dict[str, Any], transcript: str) -> list[str]:
    """Pick the deterministic target list for the probe.

    Precedence (highest wins):
        1. ``inputs['healthUrl']`` — a fully-qualified URL, used verbatim.
        2. ``inputs['host']`` — hostname, joined with ``healthPath`` (default
           ``/health``) and ``https://`` scheme. Accepts a list for both
           clusters, or a comma-separated string, or a single string.
        3. ``HEALTH_TARGETS:`` line in the LLM transcript.

    Inputs take precedence over the transcript so a Plan author can pin the
    probe to a known host and bypass discovery entirely — the most
    deterministic path.
    """
    if url := inputs.get('healthUrl'):
        if isinstance(url, str) and url.strip():
            return [url.strip()]
    hosts_raw = inputs.get('host') or inputs.get('hosts')
    health_path = str(inputs.get('healthPath') or '/health')
    if not health_path.startswith('/'):
        health_path = '/' + health_path
    hosts: list[str] = []
    if isinstance(hosts_raw, str) and hosts_raw.strip():
        hosts = [h.strip() for h in hosts_raw.split(',') if h.strip()]
    elif isinstance(hosts_raw, list):
        hosts = [str(h).strip() for h in hosts_raw if str(h).strip()]
    if hosts:
        return [_host_to_url(h, health_path) for h in hosts]
    return parse_health_targets(transcript)


def _host_to_url(host: str, health_path: str) -> str:
    """Normalise a bare host to a full URL. Idempotent for already-qualified URLs."""
    if host.startswith(('http://', 'https://')):
        # Already a URL. If it has no path, tack the health_path on.
        # Otherwise leave it verbatim — the caller specified a full URL on purpose.
        # Detect "no path" by the absence of '/' after the scheme+host.
        stripped = host.rstrip('/')
        # Trim '<scheme>://' then look for a '/'.
        after_scheme = stripped.split('://', 1)[1] if '://' in stripped else stripped
        if '/' not in after_scheme:
            return f'{stripped}{health_path}'
        return host
    return f'https://{host.rstrip("/")}{health_path}'


def _probe_one(
    url: str,
    *,
    budget_seconds: float,
    request_timeout_seconds: float,
    backoff_seconds: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    client: httpx.Client,
) -> HostProbe:
    """Retry GET url until 200, hard-fail, or budget elapses.

    Determinism: the outcome is a function of ``(url, endpoint responses,
    budget, backoff, now/sleep)``. Injected ``now``/``sleep``/``client`` make
    the whole state machine testable without real time or real HTTP.
    """
    start = now()
    deadline = start + budget_seconds
    attempts = 0
    last_status: int | None = None
    last_transport_error: str | None = None
    while True:
        attempts += 1
        try:
            response = client.get(url, timeout=request_timeout_seconds)
            last_status = response.status_code
            last_transport_error = None
            if response.status_code == 200:
                return HostProbe(
                    url=url,
                    ok=True,
                    last_status=200,
                    attempts=attempts,
                    elapsed_seconds=max(0.0, now() - start),
                )
            if response.status_code not in TRANSIENT_STATUSES:
                # Non-200, non-transient: deterministic hard fail. A 404
                # /health didn't ship a route; a 500 means the app crashed —
                # retrying won't fix either.
                return HostProbe(
                    url=url,
                    ok=False,
                    last_status=response.status_code,
                    attempts=attempts,
                    elapsed_seconds=max(0.0, now() - start),
                    reason=f'HTTP {response.status_code} from {url} (non-transient)',
                )
        except httpx.HTTPError as exc:
            # Transport errors (DNS, connect refused, TLS, timeout) during a
            # rollout are the same shape as 502/503 — the LB isn't answering
            # yet. Keep retrying within the budget.
            last_status = None
            last_transport_error = type(exc).__name__

        if now() >= deadline:
            if last_status is not None:
                last = f'HTTP {last_status}'
            elif last_transport_error is not None:
                last = last_transport_error
            else:
                last = 'no response'
            reason = f'no 200 from {url} within {budget_seconds:.0f}s (last={last}, attempts={attempts})'
            return HostProbe(
                url=url,
                ok=False,
                last_status=last_status,
                attempts=attempts,
                elapsed_seconds=max(0.0, now() - start),
                reason=reason,
            )
        sleep(backoff_seconds)


def probe_health_targets(
    targets: list[str],
    *,
    budget_seconds: float = 300.0,
    request_timeout_seconds: float = 10.0,
    backoff_seconds: float = 10.0,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Probe every target; PASS iff every one returned 200 within the budget.

    ``now`` / ``sleep`` / ``client`` are injected so tests pin the whole
    schedule without real time or real HTTP. ``budget_seconds`` is applied
    PER TARGET (so probing GCP + AZ concurrently would each get the full
    budget); the probes run sequentially in this implementation because a
    healthy cluster returns 200 in <1s, so serial cost is negligible when
    healthy, and the failure-budget cost is bounded to N × budget only in
    the pathological "everything failing" case.

    ``client`` is created + closed here when the caller doesn't pass one, so
    live callers don't leak connections; tests pass an ``httpx.Client(
    transport=httpx.MockTransport(...))`` and manage its lifetime themselves.
    """
    if not targets:
        return ProbeResult(
            verdict='FAIL',
            reason='no health targets discovered (release stages 1-3 must complete first)',
            probes=(),
        )

    close_client = False
    if client is None:
        client = httpx.Client()
        close_client = True
    try:
        probes: list[HostProbe] = []
        for url in targets:
            probes.append(
                _probe_one(
                    url,
                    budget_seconds=budget_seconds,
                    request_timeout_seconds=request_timeout_seconds,
                    backoff_seconds=backoff_seconds,
                    now=now,
                    sleep=sleep,
                    client=client,
                )
            )
    finally:
        if close_client:
            client.close()

    failed = [p for p in probes if not p.ok]
    if not failed:
        return ProbeResult(verdict='PASS', reason=None, probes=tuple(probes))
    reasons = [p.reason for p in failed if p.reason]
    return ProbeResult(
        verdict='FAIL',
        reason='; '.join(reasons) if reasons else 'one or more targets did not return 200',
        probes=tuple(probes),
    )
