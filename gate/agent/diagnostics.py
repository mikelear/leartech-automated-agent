"""Comprehensive failure-diagnostic capture for the initiative agent.

Implements the four observability layers from the
``agent-add-comprehensive-failure-diagnostics`` initiative:

Layer 1 — **error column** (one-liner classified reason).
  ``classify_failure(exc, *, last_turn_count, max_turns) -> str`` maps
  any failure shape to a terse one-line reason using the existing
  ``gate.agent.step_failure_diagnosis`` vocabulary where possible, plus
  a small set of run-level reasons (``clone_failed``,
  ``agent_sdk_error``, ``silent_terminate``, …) for things outside that
  module's per-pipeline scope.

  ``async write_failure_reason(run_id, reason)`` persists it to
  ``initiative_runs.error``.

Layer 2 — **decision log** (per-turn inflection points).
  ``async record_decision(run_id, kind, summary, payload=None,
  turn_index=None)`` appends one row to ``agent_run_decisions``. The
  module tracks a process-wide running turn counter so callers can
  omit ``turn_index`` and get monotonically-increasing values.

Layer 3 — **conversation snapshot** (full forensics).
  ``async persist_conversation_snapshot(run_id, messages, *,
  terminal_reason)`` shape-normalises SDK message objects and UPSERTs
  into ``agent_run_snapshots``. The shape transform is lenient — any
  object that the SDK may emit (AssistantMessage / UserMessage /
  ResultMessage / dict / dataclass) is converted to a JSON-safe dict
  with role + content fields.

Layer 4 — **SIGTERM/atexit handler** (last-gasp flush).
  ``install_terminate_handler(run_id, state)`` registers a SIGTERM
  signal handler + an ``atexit`` belt-and-braces hook that:
    - flushes any buffered decisions (Layer 2)
    - persists whatever conversation history is in-flight (Layer 3)
    - writes ``silent_terminate: SIGTERM received at turn N`` to
      ``initiative_runs.error`` (Layer 1)

  ``state`` is a mutable container the SDK loop updates as it runs.
  The handler reads from it at fire time so it always sees the latest
  state, not whatever was captured at install time.

All write paths are crash-tolerant — observability MUST NOT block the
SDK loop. Every helper catches its own exceptions and logs them; the
agent's primary mission is the SDK loop and a failed diagnostics
write is never a reason to abort.

DB-less mode (no ``LEARTECH_INITIATIVE_DB_DSN`` configured) is the
laptop CLI run path: every helper becomes a no-op and returns False
quietly. Same pattern as ``gate.agent.run_driver.mark_first_turn``.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import signal
import threading
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from sqlalchemy import update as sa_update

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.agent_diagnostics import insert_decision, upsert_snapshot
from app.db.models import InitiativeRunRow
from gate.agent.step_failure_diagnosis import (
    CLASSIFICATIONS,
    classify_step_failure,
)

logger = logging.getLogger(__name__)


# ─── Layer 1 — failure reason vocabulary ────────────────────────────────

# Run-level reasons (outside the per-pipeline-step taxonomy in
# ``step_failure_diagnosis.CLASSIFICATIONS``). These cover the failure
# modes the run-driver itself observes — the SDK loop crashing, the
# pod terminating, the consumer-repo clone failing, etc.
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


async def write_failure_reason(run_id: str | None, reason: str) -> bool:
    """Persist a one-line classified reason to ``initiative_runs.error``.

    Returns True iff the column was actually written (DB enabled + row
    exists + write succeeded). False quietly otherwise — the laptop
    CLI mode has no DB and we don't want to noisify the run-driver's
    stderr with "row not found" messages.

    Caller is expected to have computed ``reason`` via
    ``classify_failure`` or ``classify_step_failure_reason``. Free-form
    reasons are accepted but a warning is logged when the prefix isn't
    in ``ALL_REASONS`` — catches typos at write time.
    """
    if not run_id:
        return False
    if not is_db_enabled():
        return False

    prefix = reason.split(':', 1)[0].strip()
    if prefix and prefix not in ALL_REASONS:
        logger.warning(
            'write_failure_reason: unknown reason prefix %r (valid: %s)',
            prefix,
            sorted(ALL_REASONS),
        )

    try:
        async with db_session() as sess:
            result = await sess.execute(
                sa_update(InitiativeRunRow).where(InitiativeRunRow.id == run_id).values(error=reason)
            )
            rowcount = getattr(result, 'rowcount', 0) or 0
            return rowcount > 0
    except Exception as exc:  # noqa: BLE001 — diagnostics must never block the loop
        logger.warning('write_failure_reason failed for %s: %s', run_id, exc)
        return False


# ─── Layer 2 — decision log ─────────────────────────────────────────────


@dataclass
class _TurnCounter:
    """Process-wide running turn counter for a single run.

    The SDK reports ``num_turns`` only on ``ResultMessage``, which fires
    once per end-of-turn. Callers that record decisions BETWEEN turn
    boundaries (tool_use, gate runs, retries) need a stable index they
    can use without parsing the SDK message stream. This counter is
    bumped explicitly by the run-driver at the right inflection point.
    """

    value: int = 0


_turn_counters: dict[str, _TurnCounter] = {}
_turn_counter_lock = threading.Lock()


def reset_turn_counter(run_id: str) -> None:
    """Test helper — clear the per-run counter."""
    with _turn_counter_lock:
        _turn_counters.pop(run_id, None)


def bump_turn_counter(run_id: str) -> int:
    """Increment + return the current turn index for ``run_id``.

    Called by the run-driver from the same detection point that bumps
    its own turn-count log (per ResultMessage). Returns the
    post-increment value so callers can pass it straight through to
    ``record_decision(..., turn_index=bump_turn_counter(run_id))``.
    """
    with _turn_counter_lock:
        counter = _turn_counters.setdefault(run_id, _TurnCounter())
        counter.value += 1
        return counter.value


def current_turn_index(run_id: str) -> int:
    """Read-only view of the running counter (returns 0 if never bumped)."""
    with _turn_counter_lock:
        counter = _turn_counters.get(run_id)
        return counter.value if counter is not None else 0


async def record_decision(
    run_id: str | None,
    kind: str,
    summary: str,
    *,
    payload: Any | None = None,
    turn_index: int | None = None,
) -> bool:
    """Append one decision row to the Layer 2 table.

    No-ops cleanly when ``run_id`` is None or DB is disabled — keeps
    the laptop CLI path zero-cost. Crash-tolerant: any DB error is
    logged + swallowed (observability must not abort the SDK loop).

    Returns True iff the row was actually written.

    ``turn_index`` defaults to the current value of the in-process
    turn counter (``current_turn_index``). Pass an explicit value when
    you need to record a decision against a turn that hasn't been
    bumped yet (rare — usually only for synthetic "wait" markers at
    turn 0).
    """
    if not run_id or not is_db_enabled():
        return False

    idx = turn_index if turn_index is not None else current_turn_index(run_id)
    # Trim summary defensively — TEXT column has no hard limit but
    # operator readability degrades past ~2KB.
    if len(summary) > 2000:
        summary = summary[:1997] + '...'

    try:
        async with db_session() as sess:
            await insert_decision(
                sess,
                run_id=run_id,
                turn_index=idx,
                kind=kind,
                summary=summary,
                payload=payload,
            )
        return True
    except Exception as exc:  # noqa: BLE001 — must not block the loop
        logger.warning('record_decision failed for %s: %s', run_id, exc)
        return False


# ─── Layer 3 — conversation snapshot ────────────────────────────────────


def _normalise_message(msg: Any) -> dict[str, Any]:
    """Convert an arbitrary SDK message object into a JSON-safe dict.

    The SDK ships a handful of message classes
    (``AssistantMessage`` / ``UserMessage`` / ``ResultMessage`` / etc.)
    whose ``.content`` may be a string, a list of ``ContentBlock``
    subclasses, or None. Persisting them verbatim would fail the JSON
    serialiser; we normalise to a stable shape:

        {
            'role': 'assistant' | 'user' | 'result' | '<unknown>',
            'class': '<original class name>',
            'content': <serialisable representation>,
            'extras': {...other top-level attributes that JSON-serialise...},
        }

    Lenient — any unexpected shape lands as ``{'class': str(type), 'repr': repr(msg)}``
    rather than raising. The point of the snapshot is forensic recovery; partial
    data beats a write failure.
    """
    if msg is None:
        return {'role': 'unknown', 'class': 'NoneType', 'content': None}

    cls_name = type(msg).__name__

    # Plain dict — already a normalised shape from a previous serialiser.
    if isinstance(msg, dict):
        return msg

    # Dataclass — asdict() recursively converts to nested dicts.
    if is_dataclass(msg) and not isinstance(msg, type):
        try:
            return {'role': _role_for(cls_name), 'class': cls_name, 'content': asdict(msg)}
        except (TypeError, ValueError):
            pass

    # Generic object with .role + .content attributes (SDK message shape).
    role = _role_for(cls_name)
    content_value = getattr(msg, 'content', None)
    content_repr = _normalise_content(content_value)

    extras: dict[str, Any] = {}
    # Common SDK fields worth preserving on ResultMessage:
    for attr in ('num_turns', 'total_cost_usd', 'usage', 'is_error', 'stop_reason', 'duration_ms'):
        value = getattr(msg, attr, None)
        if value is not None:
            try:
                # Probe JSON-serialisability via str — primitives + simple dicts pass.
                _ = repr(value)
                extras[attr] = (
                    value if isinstance(value, (str, int, float, bool, list, dict, type(None))) else str(value)
                )
            except Exception:  # noqa: BLE001
                extras[attr] = str(value)

    out: dict[str, Any] = {'role': role, 'class': cls_name, 'content': content_repr}
    if extras:
        out['extras'] = extras
    return out


def _role_for(class_name: str) -> str:
    """Map an SDK message class name to a canonical role string."""
    lower = class_name.lower()
    if 'assistant' in lower:
        return 'assistant'
    if 'user' in lower:
        return 'user'
    if 'result' in lower:
        return 'result'
    if 'system' in lower:
        return 'system'
    return 'unknown'


def _normalise_content(content: Any) -> Any:
    """Convert message.content into a JSON-safe representation."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[dict[str, Any]] = []
        for block in content:
            out.append(_normalise_block(block))
        return out
    # Fallback — repr it.
    return repr(content)


def _normalise_block(block: Any) -> dict[str, Any]:
    """Normalise one ContentBlock into a serialisable dict."""
    if isinstance(block, dict):
        return block
    cls_name = type(block).__name__
    out: dict[str, Any] = {'block_class': cls_name}
    for attr in ('text', 'name', 'id', 'input', 'tool_use_id', 'thinking', 'content'):
        value = getattr(block, attr, None)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool, dict)):
            out[attr] = value
        elif isinstance(value, list):
            # Recurse one level for list-of-blocks (e.g. tool_result with list-of-text).
            out[attr] = [
                _normalise_block(item) if not isinstance(item, (str, int, float, bool)) else item for item in value
            ]
        else:
            out[attr] = repr(value)
    return out


@dataclass
class ConversationBuffer:
    """In-memory holding pen for SDK messages awaiting snapshot.

    The run-driver appends each message as it arrives; the SIGTERM
    handler reads the buffer at fire time to persist whatever's there.
    Thread-safe via the explicit ``lock`` — the handler may fire from
    a signal-handler context that's interleaved with the asyncio loop's
    natural append path.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, msg: Any) -> None:
        normalised = _normalise_message(msg)
        with self.lock:
            self.messages.append(normalised)

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a copy of the current message list (safe to persist)."""
        with self.lock:
            return list(self.messages)


async def persist_conversation_snapshot(
    run_id: str | None,
    buffer: ConversationBuffer | list[dict[str, Any]],
    *,
    terminal_reason: str | None,
) -> bool:
    """UPSERT the snapshot row for ``run_id``.

    Accepts either a ``ConversationBuffer`` (the SDK loop's preferred
    shape) or a pre-normalised list of dicts (test convenience).

    Returns True iff the row was written. Crash-tolerant — DB errors
    are logged + swallowed; the SDK loop continues regardless.
    """
    if not run_id or not is_db_enabled():
        return False

    if isinstance(buffer, ConversationBuffer):
        messages = buffer.snapshot()
    else:
        messages = list(buffer)

    try:
        async with db_session() as sess:
            await upsert_snapshot(
                sess,
                run_id=run_id,
                messages=messages,
                terminal_reason=terminal_reason,
            )
        return True
    except Exception as exc:  # noqa: BLE001 — must not block the loop
        logger.warning('persist_conversation_snapshot failed for %s: %s', run_id, exc)
        return False


# ─── Layer 4 — SIGTERM + atexit handler ────────────────────────────────


@dataclass
class TerminateState:
    """Mutable container the SDK loop updates as it runs.

    The SIGTERM handler reads from this at fire time, so the latest
    state (turn count, in-flight buffer, etc.) is always observable —
    not whatever was true at install time.
    """

    run_id: str | None = None
    buffer: ConversationBuffer = field(default_factory=ConversationBuffer)
    last_turn_count: int = 0
    max_turns: int | None = None
    # Set to True by the natural-terminal path so the SIGTERM handler
    # can skip its own writes when the loop already finished cleanly.
    natural_terminal_completed: bool = False
    # Set to True the first time the handler runs so SIGTERM + atexit
    # belt-and-braces don't double-write.
    handler_fired: bool = False


_install_lock = threading.Lock()
_installed_state: TerminateState | None = None
_previous_sigterm_handler: Any = None


def install_terminate_handler(state: TerminateState) -> None:
    """Register the SIGTERM signal handler + atexit hook.

    Idempotent — calling twice with different states updates the
    state pointer the handler reads from, but only one signal /
    atexit registration is performed. This matters because the
    initiative module is sometimes re-entered in long-lived test
    processes.

    Captures the previous SIGTERM handler so test cleanup
    (``uninstall_terminate_handler``) can restore it; production
    callers don't need to call uninstall (the pod is terminating).
    """
    global _installed_state, _previous_sigterm_handler

    with _install_lock:
        first_install = _installed_state is None
        _installed_state = state

        if first_install:
            try:
                _previous_sigterm_handler = signal.signal(signal.SIGTERM, _signal_handler)
            except ValueError:
                # signal.signal must be called from main thread. In test
                # contexts that may not hold; log + skip the signal
                # registration but keep the atexit hook (which is
                # thread-safe).
                logger.warning('SIGTERM handler not installed: not in main thread')
                _previous_sigterm_handler = None
            atexit.register(_atexit_handler)


def uninstall_terminate_handler() -> None:
    """Restore the previous SIGTERM handler. Test cleanup only."""
    global _installed_state, _previous_sigterm_handler
    with _install_lock:
        _installed_state = None
        if _previous_sigterm_handler is not None:
            try:
                signal.signal(signal.SIGTERM, _previous_sigterm_handler)
            except ValueError:
                pass
            _previous_sigterm_handler = None


def _signal_handler(signum: int, frame: Any) -> None:  # noqa: ARG001 — signal contract
    """SIGTERM handler — schedule the flush coroutine then return.

    Signal handlers can't ``await``; we schedule the async flush on
    the running event loop if one exists, otherwise run it
    synchronously via ``asyncio.run`` in a separate thread to avoid
    re-entering the parent loop.
    """
    logger.warning('SIGTERM received (signum=%d) — flushing diagnostics', signum)
    _fire_handler_safely(reason='silent_terminate: SIGTERM received')


def _atexit_handler() -> None:
    """Belt-and-braces — fires on normal process exit too.

    Only does work when the SDK loop did NOT cleanly terminate (the
    natural-terminal path sets ``natural_terminal_completed = True``
    and we early-return). Catches crashes that escape the loop's
    own try/except — e.g. a hard ``os._exit`` from a misbehaving
    dependency.
    """
    state = _installed_state
    if state is None or state.natural_terminal_completed or state.handler_fired:
        return
    logger.warning('atexit handler firing — agent process exiting without natural terminal')
    _fire_handler_safely(reason='silent_terminate: process exited without terminal')


def _fire_handler_safely(*, reason: str) -> None:
    """Run the flush coroutine without ever raising.

    Two execution paths:

    1. An asyncio event loop is running — schedule via
       ``asyncio.run_coroutine_threadsafe``. Used by SIGTERM in a
       cluster pod where the SDK loop is mid-await.
    2. No loop running (atexit after the loop exited) — spin a
       fresh loop in this thread via ``asyncio.run``. Acceptable
       because the process is exiting anyway.

    Both paths swallow all exceptions — the handler must never raise.
    """
    state = _installed_state
    if state is None or state.handler_fired:
        return
    state.handler_fired = True

    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        loop_running = loop.is_running()
    except RuntimeError:
        loop = None
        loop_running = False

    try:
        if loop_running and loop is not None:
            # We're in a signal handler interleaved with a running loop —
            # schedule the coroutine and DON'T wait (the loop will run
            # it on its next tick; if the pod is killed before that,
            # we've at least made best-effort).
            asyncio.run_coroutine_threadsafe(_flush(state, reason), loop)
        else:
            asyncio.run(_flush(state, reason))
    except Exception as exc:  # noqa: BLE001 — handler must never raise
        logger.warning('Terminate handler flush failed: %s', exc)


async def _flush(state: TerminateState, reason: str) -> None:
    """Async flush body — runs all three layer writes in best-effort order.

    Order matters: Layer 1 (error reason) first so even if the DB
    misbehaves on the larger Layer 3 JSON insert, the operator has
    SOMETHING. Then Layer 3 (snapshot) so the conversation is durable.
    Layer 2 has no buffered writes — every decision is INSERTed inline
    at record_decision time — so there's nothing to flush there.
    """
    run_id = state.run_id
    if not run_id:
        return

    # Compose a richer reason than the bare prefix.
    full_reason = f'{reason} at turn {state.last_turn_count}'
    if state.max_turns is not None:
        full_reason += f'/{state.max_turns}'

    await write_failure_reason(run_id, full_reason)
    await persist_conversation_snapshot(
        run_id,
        state.buffer,
        terminal_reason='sigterm',
    )
    # One last decision row so the operator's `SELECT ... FROM
    # agent_run_decisions` view captures the moment of termination.
    await record_decision(
        run_id,
        'sigterm',
        f'SIGTERM/atexit handler fired: {full_reason}',
        payload={
            'last_turn_count': state.last_turn_count,
            'max_turns': state.max_turns,
            'message_count': len(state.buffer.snapshot()),
        },
    )


__all__ = [
    'ALL_REASONS',
    'ConversationBuffer',
    'RUN_LEVEL_REASONS',
    'TerminateState',
    'bump_turn_counter',
    'classify_failure',
    'classify_step_failure_reason',
    'current_turn_index',
    'install_terminate_handler',
    'persist_conversation_snapshot',
    'record_decision',
    'reset_turn_counter',
    'uninstall_terminate_handler',
    'write_failure_reason',
]
