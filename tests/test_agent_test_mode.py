"""Tests for the shared TEST-MODE short-circuit in ``gate.agent.test_mode``.

The test-mode path lets a plan step skip the LLM/SDK loop and directly
self-report a directed phase (Succeeded/Failed). It's a diagnostic path
for exercising orchestration without burning tokens — with a CRITICAL
safety invariant: it MUST be gated behind the ``LEARTECH_AGENT_TEST_MODE_ALLOWED``
env var so a stray directive in a production plan can never accidentally
no-op a real run. These tests exercise both the module's primitives and
the wiring through the four agent entrypoints (initiative, infra, ba, main).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gate.agent import test_mode
from gate.agent.test_mode import (
    TEST_MODE_ANNOTATION_KEY,
    TEST_MODE_ANNOTATION_VALUE,
    TEST_MODE_GUARD_ENV,
    TestModeSpec,
    is_test_mode_allowed,
    parse_test_mode,
    run_test_mode,
)

# ─── Guard + parser primitives ──────────────────────────────────────────────


def test_guard_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent env → guard is OFF. This is the safe default."""
    monkeypatch.delenv(TEST_MODE_GUARD_ENV, raising=False)
    assert is_test_mode_allowed() is False


@pytest.mark.parametrize('value', ['false', 'False', 'FALSE', '0', '1', 'yes', 'True1', ' ', ''])
def test_guard_strict_true_only(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Only case-insensitive ``"true"`` unlocks test-mode — no permissive parse.

    A permissive parse is exactly the kind of accident that would let a typo
    (``TURE``, ``1``, ``yes``) quietly turn on test-mode in production. The
    guard behaves like a strict boolean.
    """
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, value)
    assert is_test_mode_allowed() is False


@pytest.mark.parametrize('value', ['true', 'True', 'TRUE', ' true ', 'TrUe'])
def test_guard_true_allows(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The literal string ``"true"`` (case-insensitive, trimmed) enables test-mode."""
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, value)
    assert is_test_mode_allowed() is True


def test_guard_can_be_supplied_by_kwarg() -> None:
    """The `env` kwarg is injectable — tests can bypass os.environ."""
    assert is_test_mode_allowed(env={TEST_MODE_GUARD_ENV: 'true'}) is True
    assert is_test_mode_allowed(env={}) is False


def test_parse_test_mode_ignored_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety invariant: flag off → testMode is IGNORED even when present."""
    monkeypatch.delenv(TEST_MODE_GUARD_ENV, raising=False)
    inputs = {'testMode': {'finishAs': 'Succeeded'}}
    assert parse_test_mode(inputs) is None


def test_parse_test_mode_parses_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag on + present testMode → parsed spec returned."""
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    inputs = {
        'testMode': {
            'finishAs': 'Succeeded',
            'prOutcome': 'merged',
            'message': 'hello',
            'delaySeconds': 3,
        }
    }
    spec = parse_test_mode(inputs)
    assert spec is not None
    assert spec.finish_as == 'Succeeded'
    assert spec.pr_outcome == 'merged'
    assert spec.message == 'hello'
    assert spec.delay_seconds == 3


def test_parse_test_mode_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag on but no testMode key → None. Doesn't crash."""
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    assert parse_test_mode({}) is None
    assert parse_test_mode(None) is None
    assert parse_test_mode({'other': 'field'}) is None


def test_parse_test_mode_returns_none_when_not_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """testMode present but not a dict → None (permissive at the boundary)."""
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    assert parse_test_mode({'testMode': 'nope'}) is None
    assert parse_test_mode({'testMode': ['a', 'b']}) is None
    assert parse_test_mode({'testMode': None}) is None


def test_parse_test_mode_raises_on_invalid_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag on + malformed testMode → ValidationError. Loud failure, no silent skip."""
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    with pytest.raises(ValidationError):
        parse_test_mode({'testMode': {'finishAs': 'Whatever'}})  # invalid Literal
    with pytest.raises(ValidationError):
        parse_test_mode({'testMode': {}})  # missing required finishAs
    with pytest.raises(ValidationError):
        parse_test_mode({'testMode': {'finishAs': 'Succeeded', 'delaySeconds': -1}})  # ge=0 violated


def test_parse_test_mode_rejects_unknown_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown fields in testMode → ValidationError. Typo-safe."""
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    with pytest.raises(ValidationError):
        parse_test_mode(
            {'testMode': {'finishAs': 'Succeeded', 'finish_as': 'Failed'}}  # typo'd field
        )


# ─── run_test_mode end-to-end ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_test_mode_succeeded_returns_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """finishAs=Succeeded → exit code 0, regardless of k8s reachability."""
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    spec = TestModeSpec(finishAs='Succeeded', prOutcome='none')
    code = await run_test_mode(spec)
    assert code == 0


@pytest.mark.asyncio
async def test_run_test_mode_failed_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """finishAs=Failed → exit code 1."""
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    spec = TestModeSpec(finishAs='Failed', prOutcome='none')
    code = await run_test_mode(spec)
    assert code == 1


@pytest.mark.asyncio
async def test_run_test_mode_delay_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    """delaySeconds triggers a sleep before the terminal self-report."""
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    spec = TestModeSpec(finishAs='Succeeded', prOutcome='none', delaySeconds=5)
    code = await run_test_mode(spec, sleep=fake_sleep)
    assert code == 0
    assert slept == [5]


@pytest.mark.asyncio
async def test_run_test_mode_no_sleep_when_delay_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """delaySeconds=None/0 → no sleep call."""
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    for spec in [
        TestModeSpec(finishAs='Succeeded', prOutcome='none'),
        TestModeSpec(finishAs='Succeeded', prOutcome='none', delaySeconds=0),
    ]:
        await run_test_mode(spec, sleep=fake_sleep)
    assert slept == []


@pytest.mark.asyncio
async def test_run_test_mode_calls_open_pr_when_pr_backed(monkeypatch: pytest.MonkeyPatch) -> None:
    """prOutcome != none AND open_pr_args set → open_pr MCP call fires."""
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    calls: list[dict[str, Any]] = []

    async def fake_open_pr(args: dict[str, Any]) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(test_mode, 'call_open_pr_via_mcp', fake_open_pr)

    spec = TestModeSpec(finishAs='Succeeded', prOutcome='merged')
    open_pr_args = {'repo': 'mikelear/x', 'base': 'main', 'head': 'feat/y', 'title': 't', 'body': 'b'}
    code = await run_test_mode(spec, open_pr_args=open_pr_args)
    assert code == 0
    assert calls == [open_pr_args]


@pytest.mark.asyncio
async def test_run_test_mode_skips_open_pr_when_pr_outcome_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prOutcome=none → open_pr is NOT called, even when args are provided."""
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    calls: list[dict[str, Any]] = []

    async def fake_open_pr(args: dict[str, Any]) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(test_mode, 'call_open_pr_via_mcp', fake_open_pr)

    spec = TestModeSpec(finishAs='Succeeded', prOutcome='none')
    open_pr_args = {'repo': 'mikelear/x', 'base': 'main', 'head': 'feat/y', 'title': 't', 'body': 'b'}
    await run_test_mode(spec, open_pr_args=open_pr_args)
    assert calls == []


@pytest.mark.asyncio
async def test_run_test_mode_skips_open_pr_when_args_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prOutcome != none but open_pr_args=None → skip open_pr (non-PR-backed step)."""
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    calls: list[dict[str, Any]] = []

    async def fake_open_pr(args: dict[str, Any]) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(test_mode, 'call_open_pr_via_mcp', fake_open_pr)

    spec = TestModeSpec(finishAs='Succeeded', prOutcome='merged')
    await run_test_mode(spec, open_pr_args=None)
    assert calls == []


@pytest.mark.asyncio
async def test_run_test_mode_stamps_annotation_and_patches_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_id + namespace are set, both the annotation and status patches fire."""
    monkeypatch.setenv('LEARTECH_RUN_ID', 'run-abc')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-test')

    stamps: list[dict[str, str]] = []
    patches: list[dict[str, Any]] = []

    async def fake_stamp(*, name: str, namespace: str) -> bool:
        stamps.append({'name': name, 'namespace': namespace})
        return True

    async def fake_patch(*, name: str, namespace: str, phase: str, message: str | None) -> bool:
        patches.append(
            {'name': name, 'namespace': namespace, 'phase': phase, 'message': message},
        )
        return True

    monkeypatch.setattr(test_mode, 'stamp_test_mode_annotation', fake_stamp)
    monkeypatch.setattr(test_mode, 'patch_agentrun_phase', fake_patch)

    spec = TestModeSpec(finishAs='Failed', prOutcome='none', message='directed failure')
    code = await run_test_mode(spec)
    assert code == 1
    assert stamps == [{'name': 'run-abc', 'namespace': 'jx-test'}]
    assert patches == [
        {'name': 'run-abc', 'namespace': 'jx-test', 'phase': 'Failed', 'message': 'directed failure'},
    ]


@pytest.mark.asyncio
async def test_run_test_mode_skips_patch_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No LEARTECH_RUN_ID / AGENT_RUN_NAMESPACE → skip annotation + phase patches.

    Exit code still reflects the plan's declared intent. This is the laptop-CLI
    path where there's no AgentRun CR to patch.
    """
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)

    stamps = 0
    patches = 0

    async def fake_stamp(**_kw: Any) -> bool:
        nonlocal stamps
        stamps += 1
        return True

    async def fake_patch(**_kw: Any) -> bool:
        nonlocal patches
        patches += 1
        return True

    monkeypatch.setattr(test_mode, 'stamp_test_mode_annotation', fake_stamp)
    monkeypatch.setattr(test_mode, 'patch_agentrun_phase', fake_patch)

    spec = TestModeSpec(finishAs='Succeeded', prOutcome='none')
    code = await run_test_mode(spec)
    assert code == 0
    assert stamps == 0
    assert patches == 0


@pytest.mark.asyncio
async def test_run_test_mode_survives_downstream_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing annotation stamp / status patch does NOT change the exit code.

    The exit code is the ONE signal that MUST reflect the plan's intent — a
    transient k8s hiccup shouldn't turn a Succeeded test-mode into a fail.
    """
    monkeypatch.setenv('LEARTECH_RUN_ID', 'run-abc')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-test')

    async def fake_stamp_raises(**_kw: Any) -> bool:
        return False

    async def fake_patch_raises(**_kw: Any) -> bool:
        return False

    monkeypatch.setattr(test_mode, 'stamp_test_mode_annotation', fake_stamp_raises)
    monkeypatch.setattr(test_mode, 'patch_agentrun_phase', fake_patch_raises)

    spec = TestModeSpec(finishAs='Succeeded', prOutcome='none')
    code = await run_test_mode(spec)
    assert code == 0


# ─── Constants ──────────────────────────────────────────────────────────────


def test_annotation_key_and_value_are_stable() -> None:
    """External tooling depends on this exact key/value. Guard against drift."""
    assert TEST_MODE_ANNOTATION_KEY == 'leartech.io/test-mode'
    assert TEST_MODE_ANNOTATION_VALUE == 'true'


def test_guard_env_name_is_stable() -> None:
    """The chart wires this exact env var. Guard against renames."""
    assert TEST_MODE_GUARD_ENV == 'LEARTECH_AGENT_TEST_MODE_ALLOWED'


# ─── Entrypoint wiring ──────────────────────────────────────────────────────


def _write_initiative(tmp_path: Path, body: str) -> Path:
    p = tmp_path / 'initiative.yaml'
    p.write_text(body)
    return p


@pytest.mark.asyncio
async def test_initiative_short_circuits_when_test_mode_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dev-agent (gate.agent.initiative) skips the SDK loop when testMode is honored.

    Proves the two invariants together: (1) no ``query()`` call fires (LLM
    skipped), (2) the return summary reflects the test-mode exit code.
    """
    from gate.agent import initiative as initiative_mod

    initiative_yaml = (
        'name: t\n'
        'repo: leartech-x\n'
        'branch: agent/t\n'
        'base: main\n'
        'goal: irrelevant — test-mode short-circuits\n'
        'testMode:\n'
        '  finishAs: Failed\n'
        '  prOutcome: none\n'
    )
    initiative_path = _write_initiative(tmp_path, initiative_yaml)

    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    monkeypatch.delenv('LEARTECH_RUN_ID', raising=False)
    monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
    # Even without an API key, test-mode should succeed (LLM path is skipped).
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    def _boom(*_args: Any, **_kw: Any) -> None:
        raise AssertionError('query() must not be called in test-mode')

    monkeypatch.setattr(initiative_mod, 'query', _boom)

    summary = await initiative_mod.run_initiative(initiative_path)
    assert summary.exit_code == 1  # Failed → 1
    # The SDK loop was skipped so no PR is resolved; the summary is minimal.
    assert summary.turns is None
    assert summary.pr_number is None


@pytest.mark.asyncio
async def test_initiative_ignores_test_mode_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Flag OFF + testMode present in YAML → the directive is IGNORED and the
    normal ANTHROPIC_API_KEY check fires. Real runs survive stray directives."""
    from gate.agent import initiative as initiative_mod

    initiative_yaml = (
        'name: t\n'
        'repo: leartech-x\n'
        'branch: agent/t\n'
        'base: main\n'
        'goal: irrelevant\n'
        'testMode:\n'
        '  finishAs: Succeeded\n'
        '  prOutcome: none\n'
    )
    initiative_path = _write_initiative(tmp_path, initiative_yaml)

    monkeypatch.delenv(TEST_MODE_GUARD_ENV, raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    summary = await initiative_mod.run_initiative(initiative_path)
    # Falls through to the normal path — no API key → exit 2.
    assert summary.exit_code == 2


@pytest.mark.asyncio
async def test_infra_agent_short_circuits_when_test_mode_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """infra_agent.run_infra_task honors testMode + skips query()."""
    from gate.agent import infra_agent as infra_mod

    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    def _boom(*_args: Any, **_kw: Any) -> None:
        raise AssertionError('query() must not be called in test-mode')

    monkeypatch.setattr(infra_mod, 'query', _boom)

    inputs = {
        'newRepo': 'mikelear/hello-go',
        'testMode': {'finishAs': 'Succeeded', 'prOutcome': 'none'},
    }
    code = await infra_mod.run_infra_task('create-repo', inputs)
    assert code == 0


@pytest.mark.asyncio
async def test_infra_agent_ignores_test_mode_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off → infra_agent runs normally (returns 2 for missing API key)."""
    from gate.agent import infra_agent as infra_mod

    monkeypatch.delenv(TEST_MODE_GUARD_ENV, raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    inputs = {'newRepo': 'x', 'testMode': {'finishAs': 'Succeeded', 'prOutcome': 'none'}}
    code = await infra_mod.run_infra_task('create-repo', inputs)
    assert code == 2  # normal missing-API-key path


@pytest.mark.asyncio
async def test_ba_agent_short_circuits_when_test_mode_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ba_agent.run_ba_task honors testMode + skips query().

    Brief uses ``extra='allow'`` so testMode slips through as an extra key
    and gets read out via ``model_dump(by_alias=True)``.
    """
    from gate.agent import ba_agent as ba_mod

    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    def _boom(*_args: Any, **_kw: Any) -> None:
        raise AssertionError('query() must not be called in test-mode')

    monkeypatch.setattr(ba_mod, 'query', _boom)

    brief = ba_mod.Brief.model_validate(
        {
            'name': 'test-brief',
            'goal': 'irrelevant — test-mode short-circuits',
            'successCriteria': ['deploy X healthy'],
            'testMode': {'finishAs': 'Succeeded', 'prOutcome': 'none'},
        },
    )
    code = await ba_mod.run_ba_task(brief)
    assert code == 0


@pytest.mark.asyncio
async def test_main_short_circuits_when_test_mode_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review_pr (gate.agent.main) honors testMode via LEARTECH_INITIATIVE_YAML env.

    The review agent has no dedicated inputs surface but shares the same
    short-circuit contract with its siblings — a plan step that runs review
    in test-mode passes the directive via the standard env var.
    """
    from gate.agent import main as main_mod

    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(
        'LEARTECH_INITIATIVE_YAML',
        json.dumps({'testMode': {'finishAs': 'Failed', 'prOutcome': 'none'}}),
    )

    def _boom(*_args: Any, **_kw: Any) -> None:
        raise AssertionError('query() must not be called in test-mode')

    monkeypatch.setattr(main_mod, 'query', _boom)

    code = await main_mod.review_pr('mikelear/x', 42)
    assert code == 1


@pytest.mark.asyncio
async def test_main_ignores_test_mode_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off → review_pr runs normally (returns 2 for missing API key)."""
    from gate.agent import main as main_mod

    monkeypatch.delenv(TEST_MODE_GUARD_ENV, raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(
        'LEARTECH_INITIATIVE_YAML',
        json.dumps({'testMode': {'finishAs': 'Succeeded', 'prOutcome': 'none'}}),
    )
    code = await main_mod.review_pr('mikelear/x', 42)
    assert code == 2


def test_main_env_reader_handles_junk() -> None:
    """The env-JSON reader in main is defensive against malformed content."""
    # Not JSON → None
    import os as _os

    from gate.agent import main as main_mod

    old = _os.environ.get('LEARTECH_INITIATIVE_YAML')
    try:
        _os.environ['LEARTECH_INITIATIVE_YAML'] = 'not-json'
        assert main_mod._read_test_mode_inputs_from_env() is None
        # JSON but not a dict → None
        _os.environ['LEARTECH_INITIATIVE_YAML'] = '["a", "b"]'
        assert main_mod._read_test_mode_inputs_from_env() is None
        # Valid JSON dict → returned as-is
        _os.environ['LEARTECH_INITIATIVE_YAML'] = '{"testMode": {"finishAs": "Succeeded"}}'
        result = main_mod._read_test_mode_inputs_from_env()
        assert result == {'testMode': {'finishAs': 'Succeeded'}}
    finally:
        if old is not None:
            _os.environ['LEARTECH_INITIATIVE_YAML'] = old
        else:
            _os.environ.pop('LEARTECH_INITIATIVE_YAML', None)


# ─── Initiative loader wiring ───────────────────────────────────────────────


def test_initiative_loader_accepts_test_mode_field(tmp_path: Path) -> None:
    """The Initiative model accepts a top-level ``testMode`` alias."""
    from gate.initiatives import load_initiative

    body = (
        'name: t\n'
        'repo: leartech-x\n'
        'branch: agent/t\n'
        'goal: g\n'
        'testMode:\n'
        '  finishAs: Succeeded\n'
        '  prOutcome: merged\n'
        '  message: hello\n'
        '  delaySeconds: 2\n'
    )
    p = tmp_path / 'i.yaml'
    p.write_text(body)
    initiative = load_initiative(p)
    assert initiative.test_mode == {
        'finishAs': 'Succeeded',
        'prOutcome': 'merged',
        'message': 'hello',
        'delaySeconds': 2,
    }


def test_initiative_loader_test_mode_defaults_to_none(tmp_path: Path) -> None:
    """No testMode in YAML → Initiative.test_mode is None (default)."""
    from gate.initiatives import load_initiative

    body = 'name: t\nrepo: leartech-x\nbranch: agent/t\ngoal: g\n'
    p = tmp_path / 'i.yaml'
    p.write_text(body)
    initiative = load_initiative(p)
    assert initiative.test_mode is None


# ─── open_pr helper builder ─────────────────────────────────────────────────


def test_open_pr_args_builder_captures_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """maybe_open_pr_args_for_initiative pulls run_id + namespace from env."""
    monkeypatch.setenv('LEARTECH_RUN_ID', 'run-xyz')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
    args = test_mode.maybe_open_pr_args_for_initiative(
        qualified_repo='mikelear/foo',
        base_branch='main',
        head_branch='agent/foo',
        title='hello',
        body='body',
    )
    assert args == {
        'run_id': 'run-xyz',
        'namespace': 'jx-staging',
        'repo': 'mikelear/foo',
        'base': 'main',
        'head': 'agent/foo',
        'title': 'hello',
        'body': 'body',
    }


# ─── Small integration — the whole flow through the module ─────────────────


@pytest.mark.asyncio
async def test_end_to_end_shape_with_all_features(monkeypatch: pytest.MonkeyPatch) -> None:
    """One integrated flow: parse → run → assert side-effects (all fakes)."""
    monkeypatch.setenv(TEST_MODE_GUARD_ENV, 'true')
    monkeypatch.setenv('LEARTECH_RUN_ID', 'r')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'ns')

    stamps: list[Any] = []
    patches: list[Any] = []
    open_pr_calls: list[Any] = []
    slept: list[float] = []

    async def fake_stamp(**kw: Any) -> bool:
        stamps.append(kw)
        return True

    async def fake_patch(**kw: Any) -> bool:
        patches.append(kw)
        return True

    async def fake_open_pr(args: dict[str, Any]) -> bool:
        open_pr_calls.append(args)
        return True

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(test_mode, 'stamp_test_mode_annotation', fake_stamp)
    monkeypatch.setattr(test_mode, 'patch_agentrun_phase', fake_patch)
    monkeypatch.setattr(test_mode, 'call_open_pr_via_mcp', fake_open_pr)

    spec = parse_test_mode(
        {
            'testMode': {
                'finishAs': 'Succeeded',
                'prOutcome': 'awaiting',
                'message': 'thanks',
                'delaySeconds': 4,
            }
        },
    )
    assert spec is not None
    code = await run_test_mode(
        spec,
        open_pr_args={'repo': 'mikelear/x', 'base': 'main', 'head': 'a', 'title': 't', 'body': 'b'},
        sleep=fake_sleep,
    )
    assert code == 0
    assert stamps == [{'name': 'r', 'namespace': 'ns'}]
    assert patches == [{'name': 'r', 'namespace': 'ns', 'phase': 'Succeeded', 'message': 'thanks'}]
    assert open_pr_calls == [
        {'repo': 'mikelear/x', 'base': 'main', 'head': 'a', 'title': 't', 'body': 'b'},
    ]
    assert slept == [4]


# ─── Silences the linter for stability of asyncio import ────────────────────

_ = asyncio  # keep asyncio referenced so lint doesn't drop the import
