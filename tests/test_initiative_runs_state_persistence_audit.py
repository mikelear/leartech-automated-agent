"""Parallel audit to orchestrator's plan_steps persistence audit.

The orchestrator side (tests/test_plan_steps_state_persistence_audit.py
in leartech-orchestrator) caught a bug class where the plan-runner Claude
SDK agent wrote state via Bash psql OUTSIDE the AsyncSession — leaving
plan_steps NULL when the agent crashed mid-update.

The agent side does NOT have this bug today because the run-driver IS
Python code that holds the AsyncSession directly — all writes go through
SQLAlchemy. These tests lock that property in so future drift can't
reintroduce the asymmetry.

If any of these tests FAIL, it likely means someone added a new
initiative_runs lifecycle event whose write path lives outside Python.
That's exactly the kind of regression worth catching at PR time.

Surfaced 2026-06-10 during manual debugging session that followed V5d.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _grep_multiline(pattern: str, *dirs: str) -> list[str]:
    """Return non-test production matches across multi-line patterns."""
    matches = []
    for d in dirs:
        for path in (REPO / d).rglob("*.py"):
            if "/tests/" in str(path):
                continue
            text = path.read_text()
            for m in re.finditer(pattern, text, re.MULTILINE | re.DOTALL):
                line_no = text[:m.start()].count("\n") + 1
                matches.append(f"{path.relative_to(REPO)}:{line_no}: {m.group(0)[:120]}")
    return matches


def test_initiative_runs_status_running_written_on_dispatch() -> None:
    """status='running' must be written by Python code (NOT Bash) when an
    initiative is dispatched.

    Today: app/routers/initiatives.py writes it inside the POST handler's
    AsyncSession. If this test fails, someone likely moved the write to
    a subprocess / Bash path, reintroducing the orchestrator-side bug.
    """
    callers = _grep_multiline(
        r"\w+\s*\([^)]*status\s*=\s*['\"]running['\"]",
        "app", "gate",
    )
    assert callers, (
        "No Python-side write of initiative_runs.status='running' found.\n"
        "This is the agent-side equivalent of the orchestrator bug we just "
        "fixed. If someone removed it, restore an in-session write inside "
        "the POST /initiatives handler."
    )


def test_initiative_runs_pr_number_written_in_python_not_bash() -> None:
    """pr_number must have at least one Python-side writer that holds
    a SQLAlchemy session — NOT a Bash-psql subprocess.

    Today: gate/agent/job_reconciler.py and gate/agent/initiative.py
    write it via the `update()` CRUD helper which uses AsyncSession.
    """
    callers = _grep_multiline(
        r"update\s*\([^)]*pr_number\s*=",
        "app", "gate",
    )
    assert callers, (
        "No Python-side write of initiative_runs.pr_number via the update() "
        "CRUD helper found.\n"
        "If the only writers are Bash psql subprocesses, we have the same "
        "architectural gap the orchestrator side just got fixed."
    )


def test_initiative_runs_finished_at_written_on_terminal() -> None:
    """finished_at must be written by Python code when an initiative_run
    reaches a terminal status (complete / failed / cancelled / orphaned).

    Today: gate/agent/job_reconciler.py at three sites (lines 386, 450, 599)
    and app/routers/initiatives.py:811 (cancel handler).
    """
    callers = _grep_multiline(
        r"\w+\s*\([^)]*finished_at\s*=",
        "app", "gate",
    )
    callers = [c for c in callers if "def " not in c]  # exclude signatures
    assert callers, (
        "No Python-side write of initiative_runs.finished_at found. Terminal "
        "lifecycle events must persist the timestamp in-session, not via "
        "Bash subprocess."
    )


def test_initiative_runs_started_executing_at_has_writer() -> None:
    """V4 stall fix landed the started_executing_at column to distinguish
    'pod stuck in ImagePullBackOff' from 'agent doing work'. The column
    must have at least one production writer or it's dead weight.

    If this test fails, the V4 fix is incomplete — the column exists in
    schema but no code populates it.
    """
    callers = _grep_multiline(
        r"\w+\s*\([^)]*started_executing_at\s*=",
        "app", "gate",
    )
    callers = [c for c in callers if "def " not in c]
    assert callers, (
        "started_executing_at column exists (V4 stall fix) but has no "
        "production writer. The Orch-can't-see-pod-problems lesson "
        "(feedback_orch_cant_see_pod_problems) depends on this column being "
        "populated when the agent SDK loop actually begins executing."
    )
