"""SDK-loop integration for the bidirectional command queue.

Pairs with :mod:`app.db.agent_run_commands` (the DB layer) and the
``POST/GET /initiatives/{run_id}/commands`` REST endpoints. The SDK
loop calls :func:`drain_commands` between turns; commands queue up in
Postgres and are applied here in submission order.

Why a separate module rather than wiring into ``gate/agent/initiative.py``:

  ``initiative.py`` is already large + churn-prone. Splitting the
  command vocabulary into its own module gives the test surface a
  natural seam — :func:`drain_commands` can be unit-tested against a
  stand-in :class:`CommandSink` without dragging in the SDK or the
  consumer-repo clone path.

The :class:`CommandSink` protocol describes what the SDK loop exposes
to the command handlers — minus the loop itself. Concretely:

  - ``request_cancel(reason)``     — graceful shutdown after this turn
  - ``set_pause(paused)``          — toggle the wait-flag the loop checks
  - ``inject_user_message(text)``  — append text to the conversation
                                    that the model sees as a user turn

The real wiring in ``initiative.py`` provides a concrete sink object
that hooks each method into the right loop primitive.

DB-disabled mode: :func:`drain_commands` no-ops cleanly when there's no
DSN (laptop CLI). Same pattern as
:mod:`gate.agent.diagnostics.record_decision` — observability +
control must never block the agent's mission.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.agent_run_commands import (
    AgentRunCommandRecord,
    ack_command,
    list_unacked_commands,
)
from gate.agent.diagnostics import record_decision

logger = logging.getLogger(__name__)


class CommandSink(Protocol):
    """The loop-side surface that command handlers operate against.

    Kept explicit (rather than passing the whole initiative state
    blob) so the test harness can substitute a fake that records
    calls without needing the SDK or any DB connection.
    """

    def request_cancel(self, reason: str) -> None:
        """Signal graceful shutdown with ``reason``.

        The loop checks the cancel flag at the next turn boundary and
        breaks out of the iterator. ``reason`` is recorded into
        ``initiative_runs.error`` (via the existing Layer 1
        diagnostics path) so operators see ``cancelled_by_operator:
        <reason>`` rather than a generic ``silent_terminate``.
        """
        ...

    def set_pause(self, paused: bool) -> None:
        """Toggle the loop's pause flag.

        When ``paused`` becomes True, the loop sleeps in short
        intervals on the same poll cadence as the command queue,
        re-draining commands each iteration. When a ``resume`` row
        arrives, this handler flips the flag and the loop continues.
        """
        ...

    def inject_user_message(self, text: str) -> None:
        """Append ``text`` to the conversation as a UserMessage.

        Uses the SDK's standard ``UserMessage`` injection — no new LLM
        infra. The model sees the injection at the next turn as if the
        operator had spoken at the same prompt boundary.
        """
        ...


@dataclass
class RecordingSink:
    """Test helper — records every invocation for assertion.

    Not used in production. Lets tests verify the drain path applies
    the right primitive without needing the real SDK loop.
    """

    cancel_calls: list[str] = field(default_factory=list)
    pause_calls: list[bool] = field(default_factory=list)
    inject_calls: list[str] = field(default_factory=list)

    def request_cancel(self, reason: str) -> None:
        self.cancel_calls.append(reason)

    def set_pause(self, paused: bool) -> None:
        self.pause_calls.append(paused)

    def inject_user_message(self, text: str) -> None:
        self.inject_calls.append(text)


def _format_cancel_reason(payload: Any | None) -> str:
    """Render the cancel reason from the command payload.

    Defaults to a generic string when the operator omits ``reason``,
    so the failure-diagnostics column always shows the
    ``cancelled_by_operator:`` prefix and the operator can grep the
    error column for it.
    """
    if not isinstance(payload, dict):
        return 'cancelled_by_operator: <no reason given>'
    reason = payload.get('reason')
    if isinstance(reason, str) and reason.strip():
        return f'cancelled_by_operator: {reason.strip()}'
    return 'cancelled_by_operator: <no reason given>'


def _format_inject_text(payload: Any | None) -> str | None:
    """Pull the guidance text out of an ``inject_guidance`` payload.

    Returns None when the text is missing or empty — the caller acks
    with an ``err:`` prefix so the operator's GET sees the rejection.
    The REST endpoint already validates this; the agent-side check
    defends against raw-SQL inserts.
    """
    if not isinstance(payload, dict):
        return None
    text = payload.get('text')
    if isinstance(text, str) and text.strip():
        return text
    return None


async def _apply_one(
    cmd: AgentRunCommandRecord,
    sink: CommandSink,
    *,
    run_id: str,
) -> tuple[bool, str]:
    """Apply a single command to the sink, returning ``(success, message)``.

    Each command is wrapped so a handler crash never bubbles up to the
    drain loop — observability + control must not block the agent's
    mission. A crash here surfaces as ``err: <ExcClass>: <msg>`` in
    the ack message, which the operator sees on their next GET.
    """
    ctype = cmd.command_type
    try:
        if ctype == 'cancel':
            reason = _format_cancel_reason(cmd.payload)
            sink.request_cancel(reason)
            await record_decision(
                run_id,
                'command',
                f'cancel requested: {reason}',
                payload={'command_id': cmd.id, 'reason': reason},
            )
            return True, f'cancel requested: {reason}'

        if ctype == 'pause':
            sink.set_pause(True)
            await record_decision(
                run_id,
                'command',
                'pause requested',
                payload={'command_id': cmd.id},
            )
            return True, 'paused'

        if ctype == 'resume':
            sink.set_pause(False)
            await record_decision(
                run_id,
                'command',
                'resume requested',
                payload={'command_id': cmd.id},
            )
            return True, 'resumed'

        if ctype == 'inject_guidance':
            text = _format_inject_text(cmd.payload)
            if text is None:
                return False, 'inject_guidance payload missing/empty text field'
            sink.inject_user_message(text)
            # Don't echo the full text into the decision payload —
            # snapshots already preserve it once it lands in the
            # conversation buffer. Just record the length so the
            # operator can correlate.
            await record_decision(
                run_id,
                'command',
                f'inject_guidance applied ({len(text)} chars)',
                payload={'command_id': cmd.id, 'text_length': len(text)},
            )
            return True, f'injected {len(text)} chars of guidance'

        return False, f'unknown command_type {ctype!r}'
    except Exception as exc:  # noqa: BLE001 — handler crash must not block the loop
        logger.warning('command %d (%s) handler failed: %s', cmd.id, ctype, exc)
        return False, f'{type(exc).__name__}: {exc}'


async def drain_commands(
    run_id: str | None,
    sink: CommandSink,
) -> int:
    """Process every unacked command for ``run_id`` and ack the result.

    Returns the number of commands processed (zero in the no-op DB
    case, which is the dominant cost path). The SDK loop calls this
    between turns; the runtime is dominated by the SELECT (one
    indexed scan) when no commands are pending — typically
    sub-millisecond per turn.

    Crash-tolerant at every step:

      - If the DB read fails, log + return 0.
      - If a handler crashes, that command gets an ``err:`` ack and
        we move on to the next.
      - If the ack write fails, the command stays unacked and the
        next drain re-applies the handler. Command handlers are
        written to be idempotent for exactly this case (cancel +
        pause + resume + inject are all naturally idempotent — the
        cancel flag is sticky, pause is set/unset, inject queues a
        UserMessage that the model already saw on the previous
        re-injection).

    Pause semantics: when a ``pause`` command arrives, the sink flips
    its pause flag. The caller (the SDK loop) is responsible for
    actually pausing — usually by sleeping in short intervals and
    re-draining until ``set_pause(False)`` is observed. This module
    does NOT block here; it just sets the flag.
    """
    if not run_id or not is_db_enabled():
        return 0

    try:
        async with db_session() as sess:
            commands = await list_unacked_commands(sess, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — observability must not block the loop
        logger.warning('drain_commands: list query failed for %s: %s', run_id, exc)
        return 0

    if not commands:
        return 0

    processed = 0
    for cmd in commands:
        success, message = await _apply_one(cmd, sink, run_id=run_id)
        try:
            async with db_session() as sess:
                await ack_command(
                    sess,
                    command_id=cmd.id,
                    success=success,
                    message=message,
                )
        except Exception as exc:  # noqa: BLE001 — ack failure is recoverable next pass
            logger.warning('drain_commands: ack write failed for cmd %d: %s', cmd.id, exc)
            # Note: we do NOT break here. Other commands in the batch
            # may still be ackable; one bad row shouldn't starve the
            # rest. The unacked one will be retried on the next drain.
        processed += 1

    return processed


# Default pause-poll interval — how often the loop re-checks the
# pause flag while paused. Short enough that resume feels responsive
# (operator action → loop continues in ≤2s); long enough that a long
# pause doesn't burn CPU.
DEFAULT_PAUSE_POLL_INTERVAL_SECONDS = 2.0


async def wait_while_paused(
    run_id: str | None,
    sink: CommandSink,
    *,
    is_paused: Any,
    poll_interval_seconds: float = DEFAULT_PAUSE_POLL_INTERVAL_SECONDS,
    max_iterations: int | None = None,
) -> int:
    """Block until ``is_paused()`` returns False, re-draining commands.

    The loop body is: drain → check pause → sleep. Each iteration
    drains pending commands so a ``resume`` (or a follow-up ``cancel``)
    arrives within at most ``poll_interval_seconds`` of being queued.

    ``is_paused`` is a callable (any zero-arg callable returning bool)
    rather than a flag so the caller can wrap the loop-side state
    however it likes — a closure over a list, a mutable dataclass,
    whatever. The protocol is just "ask: is the loop still paused?".

    ``max_iterations`` is a safety net for tests so a hung pause flag
    doesn't deadlock the test suite. In production it's left at None
    (no cap) — paused runs hit the K8s Job ``activeDeadlineSeconds``
    if they sit too long, which is the right outer bound.

    Returns the total number of commands processed during the pause
    (used by tests to assert behaviour; the loop ignores the return).
    """
    total = 0
    iters = 0
    while is_paused():
        total += await drain_commands(run_id, sink)
        if not is_paused():
            break
        await asyncio.sleep(poll_interval_seconds)
        iters += 1
        if max_iterations is not None and iters >= max_iterations:
            logger.warning(
                'wait_while_paused: max_iterations=%d reached for run %s',
                max_iterations,
                run_id,
            )
            break
    return total


__all__ = [
    'DEFAULT_PAUSE_POLL_INTERVAL_SECONDS',
    'CommandSink',
    'RecordingSink',
    'drain_commands',
    'wait_while_paused',
]
