"""Failure classification for the initiative agent.

``classify_failure(exc, *, last_turn_count, max_turns)`` maps any failure shape
to a terse one-line reason, and ``classify_step_failure_reason(step, log_tail)``
does the same for a pipeline step. Both are pure.

The DB-backed observability layers that used to live here — ``write_failure_reason``
(initiative_runs.error), ``record_decision`` (agent_run_decisions),
``persist_conversation_snapshot`` (agent_run_snapshots) and the SIGTERM/atexit
handler that flushed them — are gone. The AgentRun runtime has no DSN, so every
one of them short-circuited on ``is_db_enabled()``: the signal fired, the handler
ran, and nothing was written. What replaced them:

  turn count + spend      -> post_pr_handoff writes a checkpoint to the PR
  decisions / phase moves -> the controller's phase_transition lines in Loki
  crash trail             -> the preStop hook (gate/agent/crash_sticky.py) and
                             initiative.py's exception branch, both PR comments
"""

from __future__ import annotations

from gate.agent.step_failure_diagnosis import (
    CLASSIFICATIONS,
    classify_step_failure,
)

# ─── Layer 1 — failure reason vocabulary ────────────────────────────────

# Run-level reasons (outside the per-pipeline-step taxonomy in
# ``step_failure_diagnosis.CLASSIFICATIONS``). These cover the failure

RUN_LEVEL_REASONS: frozenset[str] = frozenset(
    {
        'clone_failed',
        'agent_sdk_error',
        'agent_sdk_max_turns_exceeded',
        'gate_timeout',
        'pr_link_missing',
        'silent_terminate',
        'unknown_failure',
    }
)

# Combined valid vocabulary — used for the `valid_reason` assertion in
# write_failure_reason. ALL_REASONS may not match exactly (callers can
# append a colon + free-form context), so we only validate the prefix.
ALL_REASONS: frozenset[str] = frozenset(CLASSIFICATIONS.keys()) | RUN_LEVEL_REASONS


def classify_failure(
    exc: BaseException | None,
    *,
    last_turn_count: int = 0,
    max_turns: int | None = None,
) -> str:
    """Classify a run-level failure into a one-line ``<reason>: <context>`` string.

    The taxonomy is the union of:
      - ``RUN_LEVEL_REASONS`` (this module) for run-driver failures
      - ``step_failure_diagnosis.CLASSIFICATIONS`` keys for Tekton-step
        failures the agent surfaces upward

    The format is always ``<reason>: <context>`` (colon-space separator)
    so a downstream SELECT can ``split(':', 1)`` to get a structured
    pair. Trailing context is free-form prose for the operator.

    Parameters
    ----------
    exc : BaseException | None
        The exception observed. May be None when classifying a
        non-exception terminal state (e.g. max_turns cap hit cleanly).
    last_turn_count : int
        The most recent ``num_turns`` reported by the SDK. Used to
        disambiguate ``silent_terminate`` (no turns at all) from
        ``agent_sdk_max_turns_exceeded`` (hit the cap).
    max_turns : int | None
        The configured turn cap; required to detect the cap-hit case.

    Returns
    -------
    str
        ``"<reason>: <context>"`` — always non-empty, never raises.
    """
    if exc is None:
        if max_turns is not None and last_turn_count >= max_turns > 0:
            return f'agent_sdk_max_turns_exceeded: hit max_turns={max_turns}'
        if last_turn_count == 0:
            return 'silent_terminate: pod terminated before first SDK turn fired'
        return 'unknown_failure: terminal state with no exception and no max-turns hit'

    if max_turns is not None and last_turn_count >= max_turns > 0:
        return f'agent_sdk_max_turns_exceeded: hit max_turns={max_turns} (SDK raised {type(exc).__name__})'

    # Generic SDK error — preserve exc class + abbreviated message.
    name = type(exc).__name__
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else '<no message>'
    # Truncate to keep the error column readable (TEXT, but operator-friendly).
    if len(msg) > 200:
        msg = msg[:197] + '...'
    return f'agent_sdk_error: {name}: {msg}'


def classify_step_failure_reason(step_name: str, log_tail: str) -> str:
    """Bridge from per-step pipeline failures to the one-line reason format.

    Wraps ``step_failure_diagnosis.classify_step_failure`` and renders
    its verdict as the same ``<reason>: <context>`` string Layer 1 uses
    everywhere else. Lets callers funnel any failure shape through one
    write path (``write_failure_reason``).
    """
    verdict = classify_step_failure(step_name, log_tail)
    snippet = log_tail.strip().splitlines()[-1] if log_tail.strip() else '<no log>'
    if len(snippet) > 160:
        snippet = snippet[:157] + '...'
    return f'{verdict.classification}: {step_name}: {snippet}'
