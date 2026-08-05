"""Tests for the end-of-run "expected a PR but none produced" fail-fast.

A dev/infra agent is a single LLM ``query()`` session that exits 0 unless it
crashes or hits max-turns. In prod an az-infra register step's agent failed to
push a PR (bot push-perms) yet exited 0 → the AgentRun false-Succeeded. This
fail-fast catches the same case at the AGENT layer: a PR-backed step
(``open_pr_args`` truthy) that finishes with NO PR on its branch forces a
non-zero exit and emits a loud, greppable Loki signal
(event="expected_pr_missing").

It complements — does not duplicate — the targetPR backstop
(``_backstop_target_pr``): the backstop fires only when a PR *does* exist on the
branch (recovering ``status.targetPR`` when ``open_pr`` was skipped); this
fail-fast fires only when *no* PR exists and one was expected.
"""

from __future__ import annotations

from typing import Any

import pytest

import gate.agent.initiative as initiative


class _EmitRecorder:
    """Captures obslog.emit calls."""

    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    def emit(self, level: str, event: str, msg: str, **fields: Any) -> None:
        self.emits.append({'level': level, 'event': event, 'msg': msg, **fields})


def _wire(monkeypatch: pytest.MonkeyPatch, rec: _EmitRecorder, *, run_name: str | None = 'run-1') -> None:
    monkeypatch.setattr(initiative.obslog, 'emit', rec.emit)
    if run_name is not None:
        monkeypatch.setenv('AGENT_RUN_NAME', run_name)
    else:
        monkeypatch.delenv('AGENT_RUN_NAME', raising=False)


def _fired(rec: _EmitRecorder) -> list[dict[str, Any]]:
    return [e for e in rec.emits if e['event'] == 'expected_pr_missing']


def test_forces_failure_when_pr_expected_and_none_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR-backed step (open_pr_args truthy) finished with no PR on its branch.
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=None,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

    # Distinct non-zero exit → AgentRun goes Failed (not Succeeded), K8s can retry.
    assert exit_code == initiative.EXPECTED_PR_MISSING_EXIT_CODE
    assert exit_code != 0

    fired = _fired(rec)
    assert len(fired) == 1
    ev = fired[0]
    assert ev['level'] == 'ERROR'
    assert ev['logger'] == 'agent.initiative'
    assert ev['run_id'] == 'run-1'
    assert ev['repo'] == 'mikelear/x'
    assert ev['branch'] == 'feat/a'
    assert ev['reason'] == 'no_pr_produced'


def test_no_op_when_pr_expected_and_pr_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # A PR exists on the branch → success is legitimate, exit unchanged, no event.
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=99,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

    assert exit_code == 0
    assert _fired(rec) == []


def test_no_op_when_pr_not_expected_and_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    # apply/check-only initiative or BA run: no PR expected → never force-fail.
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=False,
        pr_number=None,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

    assert exit_code == 0
    assert _fired(rec) == []


def test_no_op_when_pr_not_expected_even_with_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR not expected but one happens to exist → still a no-op (not our concern).
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=False,
        pr_number=42,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

    assert exit_code == 0
    assert _fired(rec) == []


def test_no_op_on_already_failed_run_with_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    # Already-failed run (exit 2) with a PR present → keep the original failure
    # code (its value carries meaning, e.g. K8s retry), no event.
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=7,
        exit_code=2,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

    assert exit_code == 2
    assert _fired(rec) == []


def test_no_op_on_already_failed_run_without_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    # Already-failed run (exit 2 — crash / max-turns / cancel), PR expected, no
    # PR → the run already reflects failure and 2 is meaningful, so DON'T
    # re-stamp it to our code (and don't emit — no false-Succeed to correct).
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=None,
        exit_code=2,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

    assert exit_code == 2
    assert _fired(rec) == []


def test_run_id_none_when_not_agentrun(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local/dev run with no AGENT_RUN_NAME: still fails (deterministic), run_id=None.
    rec = _EmitRecorder()
    _wire(monkeypatch, rec, run_name=None)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=None,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

    assert exit_code == initiative.EXPECTED_PR_MISSING_EXIT_CODE
    fired = _fired(rec)
    assert len(fired) == 1
    assert fired[0]['run_id'] is None


def test_complements_backstop_mutually_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    # The two end-of-run PR checks key off opposite states of ``pr_number``:
    #   * backstop fires only when a PR EXISTS (pr_number is not None)
    #   * this fail-fast fires only when NO PR exists (pr_number is None)
    # so at most one fires for any given run — they never double-signal.
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    # PR present → fail-fast is a no-op (backstop's territory).
    ec_present = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=123,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )
    assert ec_present == 0
    assert _fired(rec) == []

    # PR absent → fail-fast fires (never the backstop, which no-ops on None).
    ec_absent = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=None,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )
    assert ec_absent == initiative.EXPECTED_PR_MISSING_EXIT_CODE
    assert len(_fired(rec)) == 1
