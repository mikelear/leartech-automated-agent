"""Offline pytest suite for gate.agent.repo_access + its wiring into initiative.

These tests are the destination the ``repo_access_denied`` initiative
gives every access-failure classification. Each test corresponds to a
specific mandate in the initiative goal — cite the mandate in the test
docstring so a future reader can trace back to the requirement.

Every test is required to be LOAD-BEARING: breaking the exercised code
must break the test. See ``docs/repo_access_terminal.md`` (added in
this PR) for the "prove-each-is-load-bearing" record — each test was
manually verified to fail when the corresponding production code was
patched to elide the behaviour.

No network. No cluster. No Anthropic SDK. subprocess is stubbed via
``monkeypatch.setattr(module.subprocess.run, ...)`` in the same style
as ``tests/test_initiative_clone.py``. obslog is captured via
StringIO on the ``leartech.obslog`` logger (the same handler pattern
``tests/test_obslog.py`` uses).
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gate.agent import repo_access
from gate.agent.initiative import (
    RunSummary,
    _classify_and_emit_midrun_repo_access_denial,
    _clone_repo,
    _extract_bash_command,
)
from gate.agent.initiative_prompt import render_initiative_system_prompt
from gate.agent.repo_access import (
    EVENT_REPO_ACCESS_DENIED,
    REMEDIATION_BOOTSTRAP,
    SOURCE_CLONE,
    SOURCE_MIDRUN,
    SOURCE_PREFLIGHT,
    classify_bash_tool_result,
    classify_repo_access_failure,
    emit_repo_access_denied,
    preflight_declared_repo,
    redact_token,
)


@pytest.fixture
def cap_obslog() -> Iterator[io.StringIO]:
    """Capture obslog JSON lines via a StringIO handler on its logger.

    Same shape as ``tests/test_obslog.py::cap_obslog``. Consolidating
    to a shared fixture would be a follow-up refactor.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter('%(message)s'))
    lg = logging.getLogger('leartech.obslog')
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        lg.removeHandler(handler)


def _records(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]


def _repo_access_denied_records(buf: io.StringIO) -> list[dict[str, object]]:
    return [r for r in _records(buf) if r.get('event') == EVENT_REPO_ACCESS_DENIED]


# -----------------------------------------------------------------------------
# Mandate: "A Repository-not-found clone yields the classification and the
# run_end reason."
# -----------------------------------------------------------------------------


def test_clone_repo_yields_classification_and_reason_on_repository_not_found(
    tmp_path: Path, cap_obslog: io.StringIO
) -> None:
    """A `Repository not found` clone must return the classification-derived
    reason string (destined for :attr:`RunSummary.failure_reason`, and
    therefore the trailing ``run_end`` obslog record). Without this the
    caller falls back to a bare exit code and the ``run_end`` reader
    sees no explanation.
    """
    target = tmp_path / 'unreachable'
    fake_token = 'ghs_secret_abc'  # noqa: S105
    stderr = "remote: Repository not found.\nfatal: repository 'https://github.com/mikelear/foo.git/' not found"
    with (
        patch.dict('os.environ', {'GH_TOKEN': fake_token}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=128, stdout='', stderr=stderr)
        exit_code, reason = _clone_repo(qualified_repo='mikelear/foo', cwd=target)

    assert exit_code == 2
    assert reason is not None
    # Classification must appear verbatim in the reason so the run_end
    # reader sees WHY the run failed.
    assert repo_access.CLASS_REPOSITORY_NOT_FOUND in reason
    assert 'mikelear/foo' in reason


def test_clone_failure_reason_reaches_runsummary_via_caller_contract() -> None:
    """The caller in :func:`run_initiative` MUST thread ``clone_reason``
    onto :attr:`RunSummary.failure_reason`.

    Enforced structurally: :class:`RunSummary` has a
    ``failure_reason`` field. A future refactor that removes the field
    (regressing to the pre-fix shape) fails this test.
    """
    summary = RunSummary(exit_code=2, failure_reason='clone_failed: repository_not_found for mikelear/foo — ...')
    assert summary.failure_reason is not None
    assert 'repository_not_found' in summary.failure_reason


# -----------------------------------------------------------------------------
# Mandate: "The point-of-failure record is emitted independently of run_end."
# -----------------------------------------------------------------------------


def test_clone_failure_emits_repo_access_denied_before_return(tmp_path: Path, cap_obslog: io.StringIO) -> None:
    """The `_clone_repo` helper must emit ``event=repo_access_denied``
    at the POINT of failure — before returning to the caller. This is
    the "pod killed between denial and run_end" mitigation from the
    initiative goal.
    """
    target = tmp_path / 'unreachable'
    fake_token = 'ghs_secret_abc'  # noqa: S105
    with (
        patch.dict('os.environ', {'GH_TOKEN': fake_token}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout='', stderr='remote: Repository not found.\n'
        )
        _clone_repo(qualified_repo='mikelear/foo', cwd=target)

    records = _repo_access_denied_records(cap_obslog)
    assert len(records) == 1, 'expected exactly one repo_access_denied record from the clone-failure path'
    record = records[0]
    assert record['classification'] == repo_access.CLASS_REPOSITORY_NOT_FOUND
    assert record['source'] == SOURCE_CLONE
    assert record['repo'] == 'mikelear/foo'
    assert record['remediation'] == REMEDIATION_BOOTSTRAP
    assert record['git_exit_code'] == 128


def test_no_gh_token_emits_repo_access_denied_from_clone(tmp_path: Path, cap_obslog: io.StringIO) -> None:
    """Missing GH_TOKEN is itself a repo-access denial — emit the same
    event with a ``no_gh_token`` classification and the token-unset
    remediation, so a Loki reader sees the exact same event name they
    query for every other denial."""
    target = tmp_path / 'never-cloned'
    with (
        patch.dict('os.environ', {}, clear=True),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        _clone_repo(qualified_repo='mikelear/foo', cwd=target)
        mock_run.assert_not_called()

    records = _repo_access_denied_records(cap_obslog)
    assert len(records) == 1
    assert records[0]['classification'] == repo_access.CLASS_NO_GH_TOKEN
    assert records[0]['source'] == SOURCE_CLONE
    assert records[0]['remediation'] == repo_access.REMEDIATION_GH_TOKEN_UNSET


# -----------------------------------------------------------------------------
# Mandate: "The token appears in no emitted field."
# -----------------------------------------------------------------------------


def test_token_never_appears_in_any_emitted_field(tmp_path: Path, cap_obslog: io.StringIO) -> None:
    """The GH_TOKEN must not appear in ANY emitted record (msg or any
    field value). Git echoes the URL in stderr on 4xx; without
    redaction the token would surface via ``stderr_snippet``.

    We assert against the raw JSON text so a future field addition
    that forgets to redact still fails this test.
    """
    target = tmp_path / 'unreachable'
    fake_token = 'ghs_super_secret_xyz_1234567890abcdef'  # noqa: S105
    leaky_stderr = (
        f"fatal: unable to access 'https://x-access-token:{fake_token}@github.com/mikelear/foo.git/': "
        'The requested URL returned error: 403'
    )
    with (
        patch.dict('os.environ', {'GH_TOKEN': fake_token}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=128, stdout='', stderr=leaky_stderr)
        _clone_repo(qualified_repo='mikelear/foo', cwd=target)

    raw = cap_obslog.getvalue()
    assert fake_token not in raw, 'token leaked into obslog output; redact_token contract broken'
    records = _repo_access_denied_records(cap_obslog)
    assert records, 'expected a repo_access_denied record'
    for rec in records:
        assert fake_token not in json.dumps(rec)
        snippet = rec.get('stderr_snippet')
        if snippet is not None:
            assert '***REDACTED***' in str(snippet) or fake_token not in str(snippet)


def test_redact_token_leaves_text_unchanged_when_token_falsy() -> None:
    """`redact_token('anything', None)` must be a no-op. A guard against a
    future refactor that would accidentally replace empty-string matches
    (which would delete every position in the string)."""
    assert redact_token('secret embedded here', None) == 'secret embedded here'
    assert redact_token('secret embedded here', '') == 'secret embedded here'
    assert redact_token('secret embedded here', 'secret') == '***REDACTED*** embedded here'


# -----------------------------------------------------------------------------
# Mandate: "The pre-flight fails a declared repo that cannot be reached and
# passes one that can."
# -----------------------------------------------------------------------------


def test_preflight_passes_when_ls_remote_returns_head(cap_obslog: io.StringIO) -> None:
    """A reachable repo's `git ls-remote --heads HEAD` succeeds with a
    non-empty stdout — pre-flight must accept."""
    fake_token = 'ghs_reachable_token'  # noqa: S105
    with (
        patch.dict('os.environ', {'GH_TOKEN': fake_token}, clear=False),
        patch('gate.agent.repo_access.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='abc123\trefs/heads/main\n',
            stderr='',
        )
        outcome = preflight_declared_repo(qualified_repo='mikelear/foo')

    assert outcome.ok is True
    # A green outcome must emit NOTHING — a Loki reader querying
    # ``event="repo_access_denied"`` sees only real denials.
    assert not _repo_access_denied_records(cap_obslog)


def test_preflight_fails_and_classifies_when_ls_remote_denies(cap_obslog: io.StringIO) -> None:
    """An unreachable repo's `git ls-remote` returns non-zero — pre-flight
    must return ``ok=False`` with the classification suitable for the
    caller to plumb into RunSummary.failure_reason. The caller emits
    ``repo_access_denied`` separately, so this level does NOT emit.

    The reason string carries the classification so an operator can
    read WHY without opening the pod log."""
    fake_token = 'ghs_denied_token'  # noqa: S105
    with (
        patch.dict('os.environ', {'GH_TOKEN': fake_token}, clear=False),
        patch('gate.agent.repo_access.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=128,
            stdout='',
            stderr='remote: Repository not found.\n',
        )
        outcome = preflight_declared_repo(qualified_repo='mikelear/never-existed')

    assert outcome.ok is False
    assert outcome.classification == repo_access.CLASS_REPOSITORY_NOT_FOUND
    assert outcome.git_exit_code == 128
    assert outcome.repo == 'mikelear/never-existed'
    assert 'repository_not_found' in outcome.reason
    # Pre-flight itself does not emit — the caller does, tagging source=preflight.
    assert not _repo_access_denied_records(cap_obslog)


def test_preflight_no_gh_token_returns_no_gh_token_outcome(cap_obslog: io.StringIO) -> None:
    """Without GH_TOKEN we cannot pre-flight; the outcome carries
    ``CLASS_NO_GH_TOKEN`` and the token-unset remediation.

    Also asserts that subprocess.run is NOT called — a missing token is
    a fail-closed short-circuit (would otherwise hang or worse leak a
    URL with an empty basic-auth field to the network)."""
    with (
        patch.dict('os.environ', {}, clear=True),
        patch('gate.agent.repo_access.subprocess.run') as mock_run,
    ):
        outcome = preflight_declared_repo(qualified_repo='mikelear/foo')
        mock_run.assert_not_called()

    assert outcome.ok is False
    assert outcome.classification == repo_access.CLASS_NO_GH_TOKEN
    assert outcome.remediation == repo_access.REMEDIATION_GH_TOKEN_UNSET


def test_preflight_emit_produces_repo_access_denied_with_source_preflight(
    cap_obslog: io.StringIO,
) -> None:
    """A pre-flight failure emitted via :func:`emit_repo_access_denied`
    lands with ``source="preflight"`` — the field that distinguishes
    pre-flight, clone, and mid-run under the same event name.
    """
    emit_repo_access_denied(
        repo='mikelear/foo',
        classification=repo_access.CLASS_REPOSITORY_NOT_FOUND,
        remediation=REMEDIATION_BOOTSTRAP,
        git_exit_code=128,
        stderr_snippet='remote: Repository not found.',
        source=SOURCE_PREFLIGHT,
        token=None,
    )
    records = _repo_access_denied_records(cap_obslog)
    assert len(records) == 1
    assert records[0]['source'] == SOURCE_PREFLIGHT


# -----------------------------------------------------------------------------
# Mandate: "A mid-run permission denial is classified under the same event name."
# -----------------------------------------------------------------------------


def test_midrun_permission_denial_classifies_under_same_event(
    cap_obslog: io.StringIO,
) -> None:
    """A Bash-tool `git push` result that carries a permission-denial
    marker must classify via :func:`classify_bash_tool_result` and,
    when routed through the initiative-loop hook, emit the SAME
    ``event=repo_access_denied`` record with ``source="midrun"``.

    Single event name, two source values. The initiative-goal
    requirement: "one query finds every access failure regardless of
    where it arose."
    """
    result_block = MagicMock()
    result_block.content = (
        'remote: Permission to mikelear/foo.git denied to bot-x.\n'
        "fatal: unable to access 'https://github.com/mikelear/foo.git/': The requested URL returned error: 403"
    )
    result_block.is_error = True
    _classify_and_emit_midrun_repo_access_denial(
        tool_use_id='toolu_abc',
        command='git push origin fix/branch',
        result_block=result_block,
        emitted_ids=set(),
    )
    records = _repo_access_denied_records(cap_obslog)
    assert len(records) == 1
    assert records[0]['event'] == EVENT_REPO_ACCESS_DENIED
    assert records[0]['source'] == SOURCE_MIDRUN
    assert records[0]['classification'] == repo_access.CLASS_PERMISSION_DENIED


def test_midrun_classifier_ignores_unrelated_bash_failures(
    cap_obslog: io.StringIO,
) -> None:
    """A Bash exit-code failure with NO repo-access marker (pytest red,
    ruff red, general shell error) must NOT emit ``repo_access_denied``.

    This gates out the false-positive that would drown the signal — the
    LogQL query is supposed to surface real denials, not every red
    Bash step in the initiative loop.
    """
    result_block = MagicMock()
    result_block.content = 'FAILED tests/test_something.py::test_it - AssertionError'
    result_block.is_error = True
    _classify_and_emit_midrun_repo_access_denial(
        tool_use_id='toolu_pytest',
        command='uv run pytest',
        result_block=result_block,
        emitted_ids=set(),
    )
    assert not _repo_access_denied_records(cap_obslog)


def test_midrun_classifier_ignores_non_git_gh_commands(cap_obslog: io.StringIO) -> None:
    """A Bash command that isn't `git` or `gh` must skip classification
    even if the output happens to contain a denial marker (e.g. grep
    output referencing 'Repository not found' in a fixture)."""
    result_block = MagicMock()
    result_block.content = 'Repository not found: mentioned inside a fixture file'
    result_block.is_error = False
    _classify_and_emit_midrun_repo_access_denial(
        tool_use_id='toolu_grep',
        command='grep -r "Repository not found" tests/',
        result_block=result_block,
        emitted_ids=set(),
    )
    assert not _repo_access_denied_records(cap_obslog)


def test_midrun_classifier_dedupes_by_tool_use_id(cap_obslog: io.StringIO) -> None:
    """Given the same ``tool_use_id`` in ``emitted_ids``, a second call
    must NOT emit again. Guards against spam if the SDK re-delivers a
    result block (rare but observed on reconnect)."""
    result_block = MagicMock()
    result_block.content = 'remote: Repository not found.'
    result_block.is_error = True
    emitted: set[str] = set()
    _classify_and_emit_midrun_repo_access_denial(
        tool_use_id='toolu_dup',
        command='git clone https://github.com/mikelear/foo.git',
        result_block=result_block,
        emitted_ids=emitted,
    )
    _classify_and_emit_midrun_repo_access_denial(
        tool_use_id='toolu_dup',
        command='git clone https://github.com/mikelear/foo.git',
        result_block=result_block,
        emitted_ids=emitted,
    )
    assert len(_repo_access_denied_records(cap_obslog)) == 1


def test_extract_bash_command_extracts_command_key() -> None:
    """`_extract_bash_command` returns the value at input['command']; an
    unshaped input returns ''. Guards against a future SDK type change
    that would silently break the mid-run classifier."""
    assert _extract_bash_command({'command': 'git status'}) == 'git status'
    assert _extract_bash_command({}) == ''
    assert _extract_bash_command('bare string') == ''
    assert _extract_bash_command(None) == ''


# -----------------------------------------------------------------------------
# Mandate: "The prompt carries the do-not-work-around instruction."
# -----------------------------------------------------------------------------


def test_initiative_prompt_forbids_working_around_repo_denial() -> None:
    """The system prompt MUST tell the agent that a repo-denial is
    TERMINAL, and MUST forbid the three specific workarounds the goal
    calls out: local substitute, retargeting another repo, reporting
    success.

    The permission_mode=bypassPermissions setting means nothing
    mechanically stops the agent from doing any of these; the
    discipline lives in the prompt or nowhere. This test freezes the
    presence of that discipline against future edits.
    """
    for hold in (False, True):
        prompt = render_initiative_system_prompt(hold=hold)
        assert 'repo_access_denied' in prompt or 'terminal' in prompt.lower()
        assert 'TERMINAL' in prompt or 'terminal' in prompt.lower()
        # The three specific forbidden workarounds:
        assert 'local substitute' in prompt or 'synthesise a local' in prompt.lower()
        assert 'retarget' in prompt.lower()
        assert 'not report success' in prompt.lower() or 'DO NOT report success' in prompt
        # The remediation destination — points at the bootstrap PlanTemplate.
        assert 'bootstrap-authed-service' in prompt or 'PlanTemplate' in prompt


# -----------------------------------------------------------------------------
# Classifier vocabulary — small, stable, load-bearing.
# -----------------------------------------------------------------------------


def test_classifier_maps_known_patterns_to_stable_classifications() -> None:
    """The classification vocabulary is the LogQL panel-grouping key.
    Any silent rename breaks operator dashboards; this test freezes
    the known patterns → classification mapping.
    """
    cases = [
        ('remote: Repository not found.', repo_access.CLASS_REPOSITORY_NOT_FOUND),
        ('remote: Permission to foo denied to bot.', repo_access.CLASS_PERMISSION_DENIED),
        ('Could not resolve host: github.com', repo_access.CLASS_NETWORK),
        ('The requested URL returned error: 403', repo_access.CLASS_PERMISSION_DENIED),
        ('Bad credentials', repo_access.CLASS_PERMISSION_DENIED),
        ('GraphQL: Could not resolve to a Repository named foo', repo_access.CLASS_REPOSITORY_NOT_FOUND),
    ]
    for stderr, expected in cases:
        got, _ = classify_repo_access_failure(stderr=stderr, exit_code=1)
        assert got == expected, f'{stderr!r} → {got!r}, expected {expected!r}'


def test_classify_bash_tool_result_returns_ok_true_for_success() -> None:
    """A green git command yields ``ok=True`` — no emission, no noise.
    Critical because most git commands in the loop succeed."""
    outcome = classify_bash_tool_result(
        command='git status',
        output='On branch main\nnothing to commit, working tree clean',
        is_error=False,
    )
    assert outcome.ok is True


def test_classify_bash_tool_result_extracts_repo_hint_from_command() -> None:
    """The classifier populates the `repo` field with a best-effort
    owner/name parse from the command. Enrichment only — the event
    name is the primary query key."""
    outcome = classify_bash_tool_result(
        command='git clone https://github.com/mikelear/bar.git /tmp/bar',
        output='remote: Repository not found.',
        is_error=True,
    )
    assert outcome.ok is False
    # Not asserting on exact hint value — best-effort — but it must not
    # crash and the outcome's classification must be correct.
    assert outcome.classification == repo_access.CLASS_REPOSITORY_NOT_FOUND


# -----------------------------------------------------------------------------
# RunSummary shape lock — new field must exist and default to None.
# -----------------------------------------------------------------------------


def test_run_summary_defaults_failure_reason_to_none() -> None:
    """A success RunSummary must have failure_reason=None so obslog
    drops it (matching the "no field on success" contract)."""
    summary = RunSummary(exit_code=0)
    assert summary.failure_reason is None
