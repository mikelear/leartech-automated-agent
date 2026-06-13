"""Per-gate iteration counter + same-error escalation (v6p0.6 step 3 of 4).

Companion to :mod:`gate.watcher.iteration_loop`. That module decides
"given THIS event's failures, should we respawn / skip-infra / escalate?"
This module tracks the *history* of failures across multiple events
and answers a different question: "have we already seen this exact
failure before, and if so should the watcher stop iterating?"

Why a separate module
---------------------

``iteration_loop.decide_action`` is stateless: each call sees only the
current event. The escalation rule defined in the v6p0.6 step-3
initiative is inherently stateful — it requires comparing the current
failure's fingerprint to the *previous* attempt's fingerprint on the
same gate. Mixing state into ``decide_action`` would force the
orchestrator to thread a history dict through every call site, and
would make the pure decision logic harder to unit-test.

Instead, this module exposes:

- :class:`AttemptHistory` — a serialisable struct the orchestrator
  persists between events (catalog DB column, sticky-comment payload,
  K8s ConfigMap — the storage choice doesn't matter to us).
- :func:`record_attempt` — fold a new (gate, fingerprint) observation
  into the history. The orchestrator must call this at most once per
  (gate, watcher-cycle); see the function docstring for why the
  module itself does NOT auto-dedup.
- :func:`should_escalate` — return an :class:`EscalationReason` (or
  ``None``) describing whether the orchestrator should hand off to a
  human BEFORE spawning another agent run.
- :func:`compute_fingerprint` — derive a stable fingerprint from any of
  the failure payload kinds the watcher produces today
  (``end2end_failure``, ``gate_failure`` structured artefacts, raw log
  tail fallback).

The dispatcher (the catalog's watcher loop) calls these in order:

    fp = compute_fingerprint(payload)
    history = record_attempt(history, gate=gate, fingerprint=fp)
    reason = should_escalate(history, gate=gate)
    if reason is not None:
        apply_label('do-not-merge/manual-fix')
        post_escalation_comment(reason, history)
    else:
        # delegate to iteration_loop.decide_action() as today
        ...

Both layers must agree on the manual-fix label name; we reuse
:func:`gate.watcher.iteration_loop.manual_fix_label` rather than
hard-coding a string here.

Configuration
-------------

Two thresholds are configurable via environment variables so a
human can raise the bar without a code change when a particular gate
is genuinely flaky:

- ``LEARTECH_AGENT_SAME_ERROR_THRESHOLD`` (default 2). Number of
  consecutive identical fingerprints on the same gate that triggers
  escalation. Raising to 3+ gives the agent another retry cycle.
- ``LEARTECH_AGENT_CROSS_GATE_CAP`` (default 5). Total number of
  per-gate attempts (summed across every gate) before cross-gate
  escalation fires. Prevents whack-a-mole across many flaky gates.

The defaults are intentionally generous so legitimate multi-issue
PRs (lint + test + security-scan failing on three real bugs) iterate
through to green, but a single PR can't burn the agent's turn budget
on the same root cause repeated indefinitely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from gate.watcher.iteration_loop import manual_fix_label

logger = logging.getLogger(__name__)


# ─── Configuration ───────────────────────────────────────────────────────────


#: Per-gate threshold: number of consecutive identical fingerprints that
#: triggers same-error escalation. ``2`` is intentionally aggressive —
#: a single retry of an identical failure is fine, but two in a row is
#: a deterministic signal that the agent can't fix it on its own.
DEFAULT_SAME_ERROR_THRESHOLD: Final = 2

#: Cross-gate cap: total per-gate attempts (summed across gates) before
#: cross-gate escalation fires. ``5`` is chosen so a legitimately
#: complex PR with lint + test + security-scan + end2end + ai-review
#: each failing once still iterates through to a fix; one of those
#: failing twice with the same fingerprint trips same-error escalation
#: first, so the cross-gate cap only fires when the agent is making
#: actual progress (different fingerprints each cycle) but not
#: converging.
DEFAULT_CROSS_GATE_CAP: Final = 5

#: Env var name for the per-gate threshold override. Read at
#: ``should_escalate`` time so the operator can change it without
#: restarting the watcher pod.
ENV_SAME_ERROR_THRESHOLD: Final = 'LEARTECH_AGENT_SAME_ERROR_THRESHOLD'
ENV_CROSS_GATE_CAP: Final = 'LEARTECH_AGENT_CROSS_GATE_CAP'

#: PR comment body that operators post to clear all counters and let the
#: watcher start fresh — e.g. after fixing the underlying cluster issue
#: that was producing identical-but-transient failures.
MANUAL_RETRY_COMMAND: Final = '/retry-all'


def _read_int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to default on missing/invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            'escalation: env var %s=%r is not an int; falling back to default %d',
            name,
            raw,
            default,
        )
        return default
    if value <= 0:
        logger.warning(
            'escalation: env var %s=%d is non-positive; falling back to default %d',
            name,
            value,
            default,
        )
        return default
    return value


def same_error_threshold() -> int:
    """Current per-gate threshold (env-override or default)."""
    return _read_int_env(ENV_SAME_ERROR_THRESHOLD, DEFAULT_SAME_ERROR_THRESHOLD)


def cross_gate_cap() -> int:
    """Current cross-gate cap (env-override or default)."""
    return _read_int_env(ENV_CROSS_GATE_CAP, DEFAULT_CROSS_GATE_CAP)


# ─── EscalationReason ────────────────────────────────────────────────────────


class EscalationReason(StrEnum):
    """Why the orchestrator should stop iterating and hand off to a human.

    Subclasses :class:`enum.StrEnum` so the value JSON-serialises
    transparently for the watcher's structured logging and so callers
    can compare against the bare string.
    """

    #: A single gate has now failed with the same fingerprint
    #: ``same_error_threshold`` times in a row. The agent's last spawn
    #: didn't move the needle; another respawn will produce the same
    #: result. Hand off to a human.
    SAME_ERROR_REPEATED = 'same_error_repeated'

    #: The watcher has now accumulated ``cross_gate_cap`` per-gate
    #: attempts in total on this PR (summed across every gate). Even
    #: though each individual fingerprint may be different, the agent
    #: isn't converging — playing whack-a-mole across flaky gates.
    CROSS_GATE_BUDGET_EXHAUSTED = 'cross_gate_budget_exhausted'


# ─── AttemptHistory ──────────────────────────────────────────────────────────


@dataclass
class AttemptHistory:
    """Persistent state the watcher threads between iteration events.

    Storage is the orchestrator's concern; we treat this as a value
    object the orchestrator hydrates from its store at the start of an
    event and persists back at the end. The dict-valued field is
    mutated in place by :func:`record_attempt` for ergonomics, but
    callers are free to make a defensive copy via :func:`replace` if
    they need immutable semantics.

    The :func:`to_dict` / :func:`from_dict` pair preserve the shape for
    a JSON round-trip, which is what every realistic storage backend
    (Postgres ``jsonb``, sticky-comment payload, K8s annotation) needs.
    """

    #: gate name → list of failure fingerprints, in chronological order
    #: (oldest first). The same fingerprint may appear multiple times
    #: when consecutive cycles produce the same failure; that's the
    #: signal :func:`should_escalate` looks for.
    gate_attempts: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total_attempts(self) -> int:
        """Total per-gate attempts summed across every gate.

        This is what the cross-gate cap is measured against — NOT the
        number of distinct gates that have ever failed. A single gate
        that's failed 5 times contributes 5 to this total just as 5
        different gates each failing once would.
        """
        return sum(len(v) for v in self.gate_attempts.values())

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable shape for storage in catalog DB / sticky."""
        return {
            'gate_attempts': {gate: list(fps) for gate, fps in self.gate_attempts.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AttemptHistory:
        """Inverse of :meth:`to_dict`. ``None``-safe for first-event hydration."""
        if not data:
            return cls()
        raw = data.get('gate_attempts') or {}
        if not isinstance(raw, dict):
            logger.warning('escalation: from_dict received non-dict gate_attempts; coercing to empty')
            return cls()
        out: dict[str, list[str]] = {}
        for gate, fps in raw.items():
            if not isinstance(gate, str):
                continue
            if isinstance(fps, list):
                out[gate] = [str(fp) for fp in fps]
        return cls(gate_attempts=out)


# ─── Fingerprint computation ─────────────────────────────────────────────────


_FP_HASH_LEN: Final = 16


def _hash(parts: list[str]) -> str:
    """sha1 the concatenated parts; truncate to a stable 16-char hex digest.

    sha1 chosen for collision-resistance among the (PR's-lifetime)
    handful of failure observations; not used for security, hence
    ``usedforsecurity=False``.
    """
    raw = '||'.join(parts).encode('utf-8')
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:_FP_HASH_LEN]


def _fp_from_findings(findings: list[Any]) -> str:
    """Hash a structured-findings list (from :class:`GateFailure.findings`).

    Sort by (rule, location, severity) so two semantically-identical
    finding lists with different ordering produce the same fingerprint.
    """
    parts: list[str] = []
    for f in findings:
        if isinstance(f, dict):
            rule = str(f.get('rule', ''))
            location = str(f.get('location', ''))
            severity = str(f.get('severity', ''))
            message = str(f.get('message', ''))
        else:
            # Tolerate Finding dataclass instances directly.
            rule = str(getattr(f, 'rule', ''))
            location = str(getattr(f, 'location', ''))
            severity = str(getattr(f, 'severity', ''))
            message = str(getattr(f, 'message', ''))
        parts.append(f'{rule}|{location}|{severity}|{message}')
    parts.sort()
    return _hash(parts)


def _fp_from_failed_tests(failed_tests: list[Any]) -> str:
    """Hash the failed-tests list from an :class:`End2EndFailure` payload.

    Uses (name, message) only — trace_url / screenshot_url are storage
    URLs that change run-to-run and would otherwise force every
    fingerprint to be unique even when the failure shape is identical.
    """
    parts: list[str] = []
    for t in failed_tests:
        if isinstance(t, dict):
            name = str(t.get('name', ''))
            message = str(t.get('message', ''))
        else:
            name = str(getattr(t, 'name', ''))
            message = str(getattr(t, 'message', ''))
        parts.append(f'{name}|{message}')
    parts.sort()
    return _hash(parts)


def _fp_from_log_lines(text: str, head_lines: int = 5) -> str:
    """Hash the first N lines of a raw log/message string.

    The step-log heuristic dispatcher returns text-shaped failures (no
    structured findings). We take the FIRST few lines because Tekton
    step logs typically lead with the error summary; tail noise (stack
    traces with addresses, timestamps) varies between runs and would
    pollute the fingerprint.
    """
    if not text:
        return _hash([''])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    head = lines[:head_lines]
    return _hash(head)


def compute_fingerprint(payload: dict[str, Any] | str | None) -> str:
    """Derive a stable fingerprint for any failure-payload kind.

    Recognised inputs:

    - ``None`` / empty → ``empty`` sentinel hash (so callers don't have
      to None-guard; matching emptiness IS a valid same-error signal).
    - ``str`` → treated as a raw step-log tail; hashes first 5 lines.
    - ``dict`` with ``kind='end2end_failure'`` → hashes the
      ``failed_tests`` array (name+message).
    - ``dict`` with ``kind='gate_failure'`` → hashes the ``findings``
      array (rule+location+severity+message).
    - ``dict`` with ``raw_log_tail`` → falls back to log-line hashing
      (covers gate_failure with empty findings).
    - any other ``dict`` → JSON-canonical hash of the whole dict (last
      resort; stable but coarse).

    The same payload must always produce the same fingerprint; that's
    the property :func:`should_escalate` relies on.
    """
    if not payload:
        return _hash(['<empty>'])

    if isinstance(payload, str):
        return _fp_from_log_lines(payload)

    if not isinstance(payload, dict):
        # Defensive: stringify and hash.
        return _fp_from_log_lines(str(payload))

    kind = payload.get('kind', '')

    if kind == 'end2end_failure':
        failed_tests = payload.get('failed_tests') or []
        classification = str(payload.get('classification', ''))
        # Mix classification in so a real_failure with the same test
        # names as a preview_infra produces a different fingerprint.
        body = _fp_from_failed_tests(failed_tests)
        return _hash([classification, body])

    if kind == 'gate_failure':
        findings = payload.get('findings') or []
        if findings:
            return _fp_from_findings(findings)
        # Empty findings → fall back to raw_log_tail when available.
        tail = str(payload.get('raw_log_tail', ''))
        if tail:
            return _fp_from_log_lines(tail)

    if 'raw_log_tail' in payload:
        return _fp_from_log_lines(str(payload['raw_log_tail']))

    # Last resort — JSON-canonicalise the dict and hash. Stable but
    # coarse; mostly useful when an unrecognised payload kind shows up
    # and we still want to be able to detect "same payload twice".
    try:
        canonical = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(payload)
    return _hash([canonical])


# ─── record_attempt / mark_gate_passed / reset_all ───────────────────────────


def record_attempt(
    history: AttemptHistory,
    *,
    gate: str,
    fingerprint: str,
) -> AttemptHistory:
    """Append ``fingerprint`` to ``history.gate_attempts[gate]``.

    Returns ``history`` (same object, mutated) for fluent chaining.

    **Semantics of "once per cycle"**. The initiative goal calls out
    "same fingerprint within one watcher cycle is one attempt, not N":
    a single watcher cycle may observe the SAME failed gate from
    multiple sources (results.json from the gate task + sticky-comment
    scan + Tekton check list), and we don't want each surfacing to
    count as a separate attempt. That dedup is the orchestrator's
    responsibility — it consolidates per-gate observations BEFORE
    calling ``record_attempt`` exactly once per (gate, watcher-cycle).

    We deliberately do NOT dedup here on "last entry equal to
    incoming": across cycles, two consecutive identical fingerprints
    ARE the signal :func:`should_escalate` looks for, and silently
    swallowing them would defeat the entire feature. The contract is
    therefore "call me once per cycle"; tests cover both honoured and
    abused call patterns so a future caller can see what shape is
    expected.
    """
    history.gate_attempts.setdefault(gate, []).append(fingerprint)
    return history


def mark_gate_passed(history: AttemptHistory, *, gate: str) -> AttemptHistory:
    """Clear ``history.gate_attempts[gate]``.

    Called by the orchestrator when a previously-failing gate flips to
    SUCCESS — the next failure on the same gate then starts a fresh
    counter rather than carrying forward the prior fingerprints (which
    may have been root-caused by something the agent has since fixed).
    """
    history.gate_attempts.pop(gate, None)
    return history


def reset_all(history: AttemptHistory) -> AttemptHistory:
    """Clear every gate's history.

    Triggered by an operator posting ``/retry-all`` on the PR (see
    :data:`MANUAL_RETRY_COMMAND`) after fixing an underlying cluster
    issue. The watcher then iterates from scratch.
    """
    history.gate_attempts.clear()
    return history


# ─── should_escalate ─────────────────────────────────────────────────────────


def should_escalate(
    history: AttemptHistory,
    *,
    gate: str,
    threshold: int | None = None,
    cap: int | None = None,
) -> EscalationReason | None:
    """Return the :class:`EscalationReason` the orchestrator should act on,
    or ``None`` to keep iterating.

    Precedence:

    1. **Same-error first**. If ``gate``'s last ``threshold`` fingerprints
       are all identical, return
       :data:`EscalationReason.SAME_ERROR_REPEATED`. This is the most
       actionable signal — the agent's last attempt didn't help.
    2. **Cross-gate budget**. Else, if ``history.total_attempts`` is at
       or past the cap, return
       :data:`EscalationReason.CROSS_GATE_BUDGET_EXHAUSTED`.
    3. **Otherwise None** — keep iterating.

    The orchestrator should call this AFTER :func:`record_attempt` so
    the current failure is included in the comparison window.

    Arguments:

    - ``threshold`` overrides :func:`same_error_threshold` for callers
      that want a non-default ceiling (mostly tests).
    - ``cap`` overrides :func:`cross_gate_cap` likewise.
    """
    effective_threshold = threshold if threshold is not None else same_error_threshold()
    effective_cap = cap if cap is not None else cross_gate_cap()

    fps = history.gate_attempts.get(gate, [])
    if len(fps) >= effective_threshold:
        tail = fps[-effective_threshold:]
        if all(fp == tail[0] for fp in tail):
            logger.info(
                'escalation: same-error threshold reached on %s (fp=%s, n=%d)',
                gate,
                tail[0],
                effective_threshold,
            )
            return EscalationReason.SAME_ERROR_REPEATED

    if history.total_attempts >= effective_cap:
        logger.info(
            'escalation: cross-gate cap reached (%d >= %d)',
            history.total_attempts,
            effective_cap,
        )
        return EscalationReason.CROSS_GATE_BUDGET_EXHAUSTED

    return None


# ─── Manual override ─────────────────────────────────────────────────────────


def is_manual_retry_command(comment_body: str | None) -> bool:
    """True iff the given PR comment body is the ``/retry-all`` chatops
    command (case-insensitive, leading/trailing whitespace tolerated).

    The orchestrator scans new PR comments each watcher cycle; when this
    returns True it calls :func:`reset_all` on the history before
    proceeding with the rest of the iteration logic.
    """
    if not comment_body:
        return False
    # Match the command anywhere on a line — operators often combine it
    # with explanatory text. Compare the first non-empty stripped line
    # against the canonical command, plus an exact-match shortcut for
    # the simplest case.
    text = comment_body.strip()
    if text.lower() == MANUAL_RETRY_COMMAND:
        return True
    for line in text.splitlines():
        if line.strip().lower() == MANUAL_RETRY_COMMAND:
            return True
    return False


# ─── Comment + label rendering ───────────────────────────────────────────────


_SAME_ERROR_MARKER: Final = '<!-- leartech-agent-watcher escalation_same_error -->'
_CROSS_GATE_MARKER: Final = '<!-- leartech-agent-watcher escalation_cross_gate -->'


def escalation_label() -> str:
    """The label applied on any escalation — same one
    :mod:`gate.watcher.iteration_loop` uses for the max-iterations and
    repeated-infra paths so Lighthouse Keeper's hold contract is uniform.
    """
    return manual_fix_label()


def escalation_comment_body(
    reason: EscalationReason,
    history: AttemptHistory,
    *,
    gate: str | None = None,
    threshold: int | None = None,
    cap: int | None = None,
) -> str:
    """Render the markdown comment paired with the manual-fix label.

    The body summarises which gates have been attempted how many times
    and (for SAME_ERROR_REPEATED) which fingerprint kept recurring on
    the cited gate. The HTML markers let a future de-dup pass locate
    prior bodies without re-parsing.
    """
    effective_threshold = threshold if threshold is not None else same_error_threshold()
    effective_cap = cap if cap is not None else cross_gate_cap()

    lines: list[str]
    if reason == EscalationReason.SAME_ERROR_REPEATED:
        cited_gate = gate or '<unknown>'
        recurring_fp = ''
        if gate and history.gate_attempts.get(gate):
            recurring_fp = history.gate_attempts[gate][-1]
        lines = [
            _SAME_ERROR_MARKER,
            f'## ⚠ Same-error escalation on `{cited_gate}`',
            '',
            (
                f'The watcher has now observed the same failure fingerprint '
                f'`{recurring_fp}` on `{cited_gate}` {effective_threshold} '
                "time(s) in a row. The agent's previous attempt(s) did not "
                'change the failure shape, so another respawn would almost '
                'certainly produce the same result.'
            ),
            '',
            f'This PR is now labelled `{escalation_label()}` and held pending '
            'human triage. To resume iterations after fixing, remove the label '
            f'or post `{MANUAL_RETRY_COMMAND}` as a PR comment (which clears '
            'the attempt counters for every gate).',
            '',
        ]
    else:  # CROSS_GATE_BUDGET_EXHAUSTED
        lines = [
            _CROSS_GATE_MARKER,
            '## ⚠ Cross-gate iteration budget exhausted',
            '',
            (
                f'The watcher has now spawned {history.total_attempts} per-gate '
                f'iteration(s) on this PR (cap: {effective_cap}). Each individual '
                'gate is producing a different fingerprint each cycle, so the '
                "agent IS making progress on each — but the PR isn't converging."
            ),
            '',
            f'This PR is now labelled `{escalation_label()}` and held pending '
            'human triage. To resume iterations after fixing, remove the label '
            f'or post `{MANUAL_RETRY_COMMAND}` as a PR comment.',
            '',
        ]

    lines.append('### Attempt history')
    lines.append('')
    if not history.gate_attempts:
        lines.append('_(no recorded attempts — history dict was empty)_')
    else:
        for gate_name, fps in sorted(history.gate_attempts.items()):
            unique = len(set(fps))
            lines.append(f'- `{gate_name}`: {len(fps)} attempt(s), {unique} unique fingerprint(s)')
            # Show the last fingerprint so the human can correlate
            # against the gate's most recent failure summary.
            if fps:
                lines.append(f'  - most recent: `{fps[-1]}`')
    return '\n'.join(lines) + '\n'


# ─── Public surface ──────────────────────────────────────────────────────────


__all__ = [
    'DEFAULT_CROSS_GATE_CAP',
    'DEFAULT_SAME_ERROR_THRESHOLD',
    'ENV_CROSS_GATE_CAP',
    'ENV_SAME_ERROR_THRESHOLD',
    'MANUAL_RETRY_COMMAND',
    'AttemptHistory',
    'EscalationReason',
    'compute_fingerprint',
    'cross_gate_cap',
    'escalation_comment_body',
    'escalation_label',
    'is_manual_retry_command',
    'mark_gate_passed',
    'record_attempt',
    'reset_all',
    'same_error_threshold',
    'should_escalate',
]
