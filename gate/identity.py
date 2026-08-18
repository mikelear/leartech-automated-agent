"""AgentRun identity snapshot — capture + strip so subprocesses never see it.

The agent process needs its own AgentRun handle to call the k8s API (patch
``status.targetPR``, stamp annotations, self-report a test-mode phase, …). Nothing
it *shells out to* should ever see that handle. A ``Bash`` tool invocation, a
``uv run pytest`` from this repo's own gate suite, a ``git`` shell, a ``jx`` CLI
call — none of those have any legitimate use for a k8s API handle scoped to the
run they live inside, and every path that mistakenly acquires one is a live
mutation of *someone else's* AgentRun (or worse, the very one hosting the shell).

This module resolves the tension in one place. At the start of
:func:`gate.agent.initiative.run_initiative` we CAPTURE the identity into an
in-memory snapshot, then REMOVE the k8s-relevant subset from :data:`os.environ`.
Subsequent same-process reads go through :func:`get_run_id`,
:func:`get_run_name`, :func:`get_namespace`, :func:`is_status_enabled` — all
backed by the snapshot post-capture, falling back to :data:`os.environ`
pre-capture (so unit tests that monkeypatch env vars and never enter
``run_initiative`` still observe those values through the same accessors).

Every ``subprocess`` the agent spawns after :func:`capture_and_strip` inherits
the STRIPPED environment, so the existing "no AgentRun identity → skip" guards
in :mod:`gate.agent.initiative` (backstop) do genuinely skip rather than merely
hope-to-skip.

``LEARTECH_RUN_ID`` is CAPTURED but not STRIPPED. It is:

- the primary correlation key obslog stamps on every log record — every
  diagnostic query in the incident-response ladder keys off this one field,
  so silently dropping it from log lines would break Loki triage; and
- harmless on its own: a subprocess seeing only the run id cannot reach the
  AgentRun CR, which needs ``AGENT_RUN_NAME`` + ``AGENT_RUN_NAMESPACE``. Every
  AgentRun status/annotation write needs BOTH ``metadata.name`` (from
  ``AGENT_RUN_NAME``) AND ``metadata.namespace`` (from ``AGENT_RUN_NAMESPACE``);
  stripping either is minimally sufficient. Stripping ``LEARTECH_AGENTRUN_STATUS``
  on top gives the existing status-reporting-disabled short-circuit, which is
  the SAME code path as the "local/dev run" branch — one guard, two triggers.

The tri-partite strip (``AGENT_RUN_NAME`` + ``AGENT_RUN_NAMESPACE`` +
``LEARTECH_AGENTRUN_STATUS``) is therefore the minimum sufficient set to make a
subprocess unable to reach the AgentRun, and the maximum we can strip while
still keeping Loki correlation intact.
"""

from __future__ import annotations

import os

_CAPTURED_VARS: tuple[str, ...] = (
    'AGENT_RUN_NAME',
    'AGENT_RUN_NAMESPACE',
    'LEARTECH_AGENTRUN_STATUS',
    'LEARTECH_RUN_ID',
)

_STRIPPED_VARS: tuple[str, ...] = (
    'AGENT_RUN_NAME',
    'AGENT_RUN_NAMESPACE',
    'LEARTECH_AGENTRUN_STATUS',
)

_snapshot: dict[str, str] = {}
_captured: bool = False


def capture_and_strip() -> None:
    """Snapshot AgentRun identity, then REMOVE the k8s-relevant subset from
    :data:`os.environ`.

    Idempotent: a second call is a no-op (the snapshot is already the source
    of truth for this process, and the stripped vars are already gone).

    MUST be called before any code path that shells out (the Claude Agent
    SDK's ``Bash`` tool, ``pytest``, ``git``, ``jx``, …). The initiative
    entrypoint calls this at the very top of
    :func:`gate.agent.initiative.run_initiative` — the one place from which
    every downstream subprocess in a real run is ultimately spawned.
    """
    global _captured
    if _captured:
        return
    for name in _CAPTURED_VARS:
        raw = os.environ.get(name)
        if raw is not None:
            _snapshot[name] = raw
    for name in _STRIPPED_VARS:
        os.environ.pop(name, None)
    _captured = True


def reset_for_tests() -> None:
    """Reset module state so a subsequent test can capture afresh.

    Pytest fixtures (see ``tests/conftest.py``) call this between tests so
    ``_captured=True`` from a prior test never leaks stale snapshot values
    into the next test's accessor reads. Production code must never invoke
    this.
    """
    global _captured
    _snapshot.clear()
    _captured = False


def is_captured() -> bool:
    """True iff :func:`capture_and_strip` has been called in this process."""
    return _captured


def _read(name: str) -> str | None:
    """Return the captured value, or fall back to :data:`os.environ` when
    :func:`capture_and_strip` has not yet been called.

    The fallback is what lets unit tests that monkeypatch env vars and never
    enter :func:`run_initiative` observe those values through the same
    accessors — no split code paths between test and prod.
    """
    if _captured:
        return _snapshot.get(name)
    return os.environ.get(name)


def get_run_id() -> str:
    """``LEARTECH_RUN_ID`` — the AgentRun ``metadata.name`` (== DB run id)."""
    return (_read('LEARTECH_RUN_ID') or '').strip()


def get_run_name() -> str:
    """``AGENT_RUN_NAME`` — the AgentRun CR ``metadata.name``."""
    return (_read('AGENT_RUN_NAME') or '').strip()


def get_namespace() -> str:
    """``AGENT_RUN_NAMESPACE`` — the AgentRun CR ``metadata.namespace``."""
    return (_read('AGENT_RUN_NAMESPACE') or '').strip()


def is_status_enabled() -> bool:
    """``LEARTECH_AGENTRUN_STATUS`` — the status-reporting opt-in flag.

    Truthy values are ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive,
    stripped). Any other value — including empty / unset — reads as False,
    which is the safe default for laptop runs and stripped subprocess envs.
    """
    return (_read('LEARTECH_AGENTRUN_STATUS') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def ambient_log_fields() -> dict[str, str]:
    """Identity fields obslog stamps on every log record.

    Only populated when the corresponding accessor is non-empty — obslog
    drops absent fields from the JSON record, matching the pre-strip
    behaviour of ``_context()``.
    """
    fields: dict[str, str] = {}
    run_id = get_run_id()
    if run_id:
        fields['run_id'] = run_id
    namespace = get_namespace()
    if namespace:
        fields['namespace'] = namespace
    return fields


__all__ = [
    'ambient_log_fields',
    'capture_and_strip',
    'get_namespace',
    'get_run_id',
    'get_run_name',
    'is_captured',
    'is_status_enabled',
    'reset_for_tests',
]
