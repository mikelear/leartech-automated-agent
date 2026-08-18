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
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    exit_code = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=None,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )

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
    rec = _EmitRecorder()
    _wire(monkeypatch, rec)

    ec_present = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=123,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )
    assert ec_present == 0
    assert _fired(rec) == []

    ec_absent = initiative._fail_fast_if_expected_pr_missing(
        pr_expected=True,
        pr_number=None,
        exit_code=0,
        qualified_repo='mikelear/x',
        branch='feat/a',
    )
    assert ec_absent == initiative.EXPECTED_PR_MISSING_EXIT_CODE
    assert len(_fired(rec)) == 1
