"""classify_failure — the agent's failure-reason vocabulary.

The DB-backed layers this module used to test (initiative_runs.error,
agent_run_decisions, agent_run_snapshots and the SIGTERM flush) are gone: the
AgentRun runtime has no DSN, so every one of them short-circuited on
is_db_enabled() and nothing was ever written. Classification is what remains,
and it is pure.
"""

from __future__ import annotations

from gate.agent.diagnostics import classify_failure, classify_step_failure_reason


def test_classify_failure_returns_silent_terminate_when_no_exception_and_no_turns() -> None:
    """No exception + no turns observed = pod terminated before first SDK turn."""
    reason = classify_failure(None, last_turn_count=0, max_turns=200)
    assert reason.startswith('silent_terminate:')
    assert 'pod terminated' in reason or 'before first SDK turn' in reason


def test_classify_failure_returns_max_turns_when_exc_is_none_and_cap_hit() -> None:
    """Cap-hit without an exception — agent exited cleanly at the ceiling."""
    reason = classify_failure(None, last_turn_count=200, max_turns=200)
    assert reason.startswith('agent_sdk_max_turns_exceeded:')
    assert '200' in reason


def test_classify_failure_classifies_max_turns_when_sdk_raised_at_ceiling() -> None:
    """SDK raises the moment max_turns is hit (issue #913). Classifier must
    prefer the cap-hit reason over the bare SDK error so the operator sees
    the actionable signal first."""
    exc = RuntimeError('Maximum number of turns reached')
    reason = classify_failure(exc, last_turn_count=200, max_turns=200)
    assert reason.startswith('agent_sdk_max_turns_exceeded:')
    # The exception class is still surfaced for traceability.
    assert 'RuntimeError' in reason


def test_classify_failure_returns_agent_sdk_error_for_generic_exception() -> None:
    """A real SDK crash mid-run gets ``agent_sdk_error: <ExcClass>: <message>``."""
    exc = ValueError('Connection reset by peer')
    reason = classify_failure(exc, last_turn_count=42, max_turns=200)
    assert reason.startswith('agent_sdk_error:')
    assert 'ValueError' in reason
    assert 'Connection reset by peer' in reason


def test_classify_failure_truncates_long_messages() -> None:
    """A 5KB stack trace shouldn't blow out the error column.

    Truncation keeps the column readable; full forensics live in the
    snapshot table (Layer 3).
    """
    exc = RuntimeError('x' * 5000)
    reason = classify_failure(exc, last_turn_count=10, max_turns=200)
    assert len(reason) < 300


def test_classify_failure_handles_unknown_terminal_shape() -> None:
    """No exception + non-zero turn count + no cap hit — defensive case."""
    reason = classify_failure(None, last_turn_count=50, max_turns=200)
    assert reason.startswith('unknown_failure:')


def test_classify_step_failure_reason_renders_canonical_format() -> None:
    """Bridge to step_failure_diagnosis: pipeline-step failures land in
    the same ``<reason>: <context>`` shape so callers funnel everything
    through one write path."""
    log_tail = 'app/foo.py:12:5: E501 line too long\nFound 1 error.\n'
    reason = classify_step_failure_reason('ruff', log_tail)
    assert reason.startswith('ruff_lint_error:')
    assert 'ruff' in reason
