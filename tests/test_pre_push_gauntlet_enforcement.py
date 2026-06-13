"""Unit tests for gate.agent.gauntlets — the pre-push gauntlet enforcement.

Covers v6p0.6 step 5:

- Language detection from build-system markers (pyproject / go.mod /
  Cargo.toml / angular.json / package.json).
- Per-language GAUNTLETS dict shape — order matters, each entry is a
  recognisable :class:`GauntletCheck`.
- :func:`run_gauntlet` invokes commands in order via the injected
  :class:`GauntletRunner`, returns ``passed=True`` only when every
  available check exited zero.
- A failing check produces a structured ``gauntlet_failure`` payload with
  the contract documented in the initiative goal (``kind``, ``check``,
  ``command``, ``output``, ``language``, ``returncode``).
- Missing toolchain (``which`` returns False) → skip + warn, don't crash.
- Operator ``/skip-gauntlet <name>`` honoured; agent-issued bypass is
  REJECTED (audit-trail invariant).
- ``gofmt -l`` special case — exit 0 + non-empty stdout means "file
  needs formatting" → failure.
- ``initiative_prompt`` cites the gauntlet at BOTH push sites (initial-PR
  step 4 AND iteration-push step 10) — the calibration goal of this
  initiative.
- ``scripts/e2e.sh`` exercises the in-process gauntlet runner so a
  packaging regression of ``gate.agent.gauntlets`` shows up at the e2e
  layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gate.agent.gauntlets import (
    GAUNTLETS,
    SKIP_LOCALLY,
    GauntletCheck,
    GauntletFailure,
    GauntletRunner,
    SkipGauntletDirective,
    _RunOutcome,
    detect_language,
    parse_skip_gauntlet_directives,
    run_gauntlet,
)

# ─── Test runner helpers ─────────────────────────────────────────────────────


@dataclass
class _FakeRunner(GauntletRunner):
    """Deterministic runner for the unit tests.

    ``outcomes`` is keyed by the first argv token (``ruff``, ``mypy``,
    ``pytest``, ``gofmt``, etc.) — the test seeds the outcome each check
    should return. Missing keys mean ``which()`` returns False (the
    toolchain isn't installed).
    """

    outcomes: dict[str, _RunOutcome] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    raise_for: set[str] = field(default_factory=set)

    def which(self, tool: str) -> bool:
        return tool in self.outcomes

    def run(self, argv: Sequence[str], *, cwd: Path, timeout: int) -> _RunOutcome:
        argv_tuple = tuple(argv)
        self.calls.append(argv_tuple)
        if argv[0] in self.raise_for:
            from gate.agent.gauntlets import RunnerError

            raise RunnerError(f'simulated runner crash on {argv[0]}')
        return self.outcomes[argv[0]]


# ─── Language detection ──────────────────────────────────────────────────────


def test_detect_language_python(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="x"\n')
    assert detect_language(tmp_path) == 'python'


def test_detect_language_go(tmp_path: Path) -> None:
    (tmp_path / 'go.mod').write_text('module example\n')
    assert detect_language(tmp_path) == 'go'


def test_detect_language_rust(tmp_path: Path) -> None:
    (tmp_path / 'Cargo.toml').write_text('[package]\nname="x"\n')
    assert detect_language(tmp_path) == 'rust'


def test_detect_language_angular_via_angular_json(tmp_path: Path) -> None:
    (tmp_path / 'angular.json').write_text('{}')
    assert detect_language(tmp_path) == 'angular'


def test_detect_language_angular_via_package_json(tmp_path: Path) -> None:
    (tmp_path / 'package.json').write_text('{}')
    assert detect_language(tmp_path) == 'angular'


def test_detect_language_unknown_returns_none(tmp_path: Path) -> None:
    """Empty dir → no marker → None (caller decides what to do)."""
    assert detect_language(tmp_path) is None


def test_detect_language_priority_pyproject_wins_over_package(tmp_path: Path) -> None:
    """Python services sometimes ship a frontend package.json; pyproject wins."""
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="x"\n')
    (tmp_path / 'package.json').write_text('{}')
    assert detect_language(tmp_path) == 'python'


# ─── GAUNTLETS dict shape ────────────────────────────────────────────────────


def test_gauntlets_dict_covers_required_languages() -> None:
    """Initiative goal mandates python + go + angular at minimum."""
    assert {'python', 'go', 'angular', 'rust'}.issubset(GAUNTLETS)


def test_python_gauntlet_has_lint_types_tests_in_order() -> None:
    checks = GAUNTLETS['python']
    names = [c.name for c in checks]
    # Format → lint → types → tests (cheap-to-expensive ordering).
    assert names == ['ruff-format', 'ruff-check', 'mypy', 'pytest']


def test_go_gauntlet_has_fmt_vet_lint_tests_in_order() -> None:
    checks = GAUNTLETS['go']
    names = [c.name for c in checks]
    assert names == ['go-fmt', 'go-vet', 'golangci-lint', 'go-test']


def test_angular_gauntlet_has_lint_and_unit_tests() -> None:
    checks = GAUNTLETS['angular']
    names = [c.name for c in checks]
    assert 'ng-lint' in names
    assert 'ng-test' in names


def test_gauntlet_check_argv_is_shlex_split() -> None:
    """Commands like `npm test -- --watch=false` must split into argv correctly."""
    c = GauntletCheck(name='x', command='npm test -- --watch=false --browsers=ChromeHeadless')
    assert c.argv == ['npm', 'test', '--', '--watch=false', '--browsers=ChromeHeadless']
    assert c.toolchain == 'npm'


def test_skip_locally_contains_cluster_only_gates() -> None:
    """Cluster-side gates must not appear in any per-language gauntlet."""
    assert {'image-scan', 'dynamic-scan', 'ai-review', 'end2end', 'end2end-ui'}.issubset(SKIP_LOCALLY)


# ─── run_gauntlet: happy path (python) ───────────────────────────────────────


def _python_repo(tmp_path: Path) -> Path:
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="x"\n')
    return tmp_path


def test_run_gauntlet_python_all_pass(tmp_path: Path) -> None:
    repo = _python_repo(tmp_path)
    runner = _FakeRunner(
        outcomes={
            'ruff': _RunOutcome(returncode=0),
            'mypy': _RunOutcome(returncode=0),
            'pytest': _RunOutcome(returncode=0),
        }
    )
    result = run_gauntlet(repo, runner=runner)
    assert result.passed is True
    assert result.failures == ()
    assert result.language == 'python'
    # ruff-format + ruff-check both invoke `ruff` → 4 calls total (2 ruff + mypy + pytest).
    assert [call[0] for call in runner.calls] == ['ruff', 'ruff', 'mypy', 'pytest']


def test_run_gauntlet_python_ruff_fails_stops_early(tmp_path: Path) -> None:
    """When ruff fails, subsequent mypy + pytest must NOT run."""
    repo = _python_repo(tmp_path)
    runner = _FakeRunner(
        outcomes={
            'ruff': _RunOutcome(returncode=1, stderr='lint.py:1:1: F401 unused import\n'),
            'mypy': _RunOutcome(returncode=0),
            'pytest': _RunOutcome(returncode=0),
        }
    )
    result = run_gauntlet(repo, runner=runner)
    assert result.passed is False
    assert len(result.failures) == 1
    f = result.failures[0]
    assert f.check == 'ruff-format'
    assert 'F401' in f.output
    assert f.returncode == 1
    # mypy + pytest were NEVER invoked (early exit on first failure).
    assert [call[0] for call in runner.calls] == ['ruff']


def test_gauntlet_failure_payload_contract(tmp_path: Path) -> None:
    """The wire-format must match the initiative goal's contract verbatim."""
    repo = _python_repo(tmp_path)
    runner = _FakeRunner(
        outcomes={
            'ruff': _RunOutcome(returncode=2, stderr='boom'),
        }
    )
    result = run_gauntlet(repo, runner=runner)
    assert not result.passed
    payload = result.failures[0].to_dict()
    assert payload == {
        'kind': 'gauntlet_failure',
        'check': 'ruff-format',
        'command': 'ruff format --check .',
        'output': 'boom',
        'language': 'python',
        'returncode': 2,
    }


def test_gauntlet_failure_fingerprint_idempotency() -> None:
    """Same check + language → same fingerprint (iteration counter idempotency)."""
    f1 = GauntletFailure(
        check='ruff-format',
        command='ruff format --check .',
        output='boom',
        language='python',
        returncode=1,
    )
    f2 = GauntletFailure(
        check='ruff-format',
        command='ruff format --check .',
        output='different output but same root cause',
        language='python',
        returncode=1,
    )
    assert f1.fingerprint() == f2.fingerprint()
    # Different language → different fingerprint (a Go ruff would be silly but
    # the key must distinguish).
    f3 = GauntletFailure(
        check='ruff-format',
        command='',
        output='',
        language='go',
        returncode=1,
    )
    assert f1.fingerprint() != f3.fingerprint()


def test_run_gauntlet_truncates_huge_output(tmp_path: Path) -> None:
    """Very long stderr is tail-truncated so it doesn't blow up the prompt budget."""
    repo = _python_repo(tmp_path)
    big = 'X' * 200_000
    runner = _FakeRunner(outcomes={'ruff': _RunOutcome(returncode=1, stderr=big)})
    result = run_gauntlet(repo, runner=runner)
    assert not result.passed
    assert len(result.failures[0].output) < 10_000  # cap was 4 KB + envelope
    assert 'truncated' in result.failures[0].output


# ─── Go-specific: gofmt -l special case ──────────────────────────────────────


def _go_repo(tmp_path: Path) -> Path:
    (tmp_path / 'go.mod').write_text('module example\n')
    return tmp_path


def test_run_gauntlet_go_gofmt_exit_zero_with_dirty_files_is_failure(tmp_path: Path) -> None:
    """`gofmt -l` exits 0 but lists unformatted files — that's a failure shape."""
    repo = _go_repo(tmp_path)
    runner = _FakeRunner(
        outcomes={
            'gofmt': _RunOutcome(returncode=0, stdout='main.go\npkg/util.go\n'),
            'go': _RunOutcome(returncode=0),
            'golangci-lint': _RunOutcome(returncode=0),
        }
    )
    result = run_gauntlet(repo, runner=runner)
    assert result.passed is False
    assert result.failures[0].check == 'go-fmt'
    assert 'main.go' in result.failures[0].output


def test_run_gauntlet_go_gofmt_exit_zero_with_empty_stdout_passes(tmp_path: Path) -> None:
    repo = _go_repo(tmp_path)
    runner = _FakeRunner(
        outcomes={
            'gofmt': _RunOutcome(returncode=0, stdout=''),
            'go': _RunOutcome(returncode=0),
            'golangci-lint': _RunOutcome(returncode=0),
        }
    )
    result = run_gauntlet(repo, runner=runner)
    assert result.passed is True


# ─── Missing toolchain — skip + warn, don't crash ────────────────────────────


def test_run_gauntlet_skips_missing_toolchain(tmp_path: Path) -> None:
    """`golangci-lint` not installed → skip with reason, don't crash."""
    repo = _go_repo(tmp_path)
    runner = _FakeRunner(
        outcomes={
            'gofmt': _RunOutcome(returncode=0, stdout=''),
            'go': _RunOutcome(returncode=0),
            # `golangci-lint` deliberately absent — which() returns False.
        }
    )
    result = run_gauntlet(repo, runner=runner)
    assert result.passed is True
    skipped_names = {name for name, _ in result.skipped}
    assert 'golangci-lint' in skipped_names
    # `go test` doesn't fail because of the missing toolchain — but `go` is
    # the toolchain for `go test` too, so it ran fine.
    assert 'go-test' in result.ran


def test_run_gauntlet_no_marker_returns_no_op(tmp_path: Path) -> None:
    """Empty repo (no pyproject / go.mod / etc.) → passed=True, no checks run."""
    runner = _FakeRunner()
    result = run_gauntlet(tmp_path, runner=runner)
    assert result.passed is True
    assert result.failures == ()
    assert result.ran == ()
    assert runner.calls == []


# ─── Operator /skip-gauntlet directive ───────────────────────────────────────


def test_parse_skip_gauntlet_simple() -> None:
    comments = [
        {
            'body': '/skip-gauntlet mypy',
            'user': {'login': 'mikelear'},
            'id': 4242,
        }
    ]
    directives = parse_skip_gauntlet_directives(comments)
    assert directives == (SkipGauntletDirective(check='mypy', actor='mikelear', comment_id=4242),)


def test_parse_skip_gauntlet_multiple_in_one_comment() -> None:
    comments = [
        {
            'body': 'reviewing:\n/skip-gauntlet ruff-format\n/skip-gauntlet mypy\n',
            'user': {'login': 'human-reviewer'},
            'id': 1,
        }
    ]
    directives = parse_skip_gauntlet_directives(comments)
    assert {d.check for d in directives} == {'ruff-format', 'mypy'}


def test_parse_skip_gauntlet_missing_check_name_is_ignored() -> None:
    """`/skip-gauntlet` without an argument is too broad — refuse it."""
    comments = [
        {'body': '/skip-gauntlet', 'user': {'login': 'mikelear'}, 'id': 1},
    ]
    assert parse_skip_gauntlet_directives(comments) == ()


def test_parse_skip_gauntlet_agent_self_bypass_is_rejected() -> None:
    """Same rule as /skip-e2e-check + /hold cancel — agent can't silence itself."""
    comments = [
        {
            'body': '/skip-gauntlet mypy',
            'user': {'login': 'leartech-agent-bot'},
            'id': 1,
        },
        {
            'body': '/skip-gauntlet pytest',
            'user': {'login': 'mikelear'},
            'id': 2,
        },
    ]
    directives = parse_skip_gauntlet_directives(comments, agent_login='leartech-agent-bot')
    # Only the human's bypass survives — agent self-bypass dropped + warned.
    assert len(directives) == 1
    assert directives[0].check == 'pytest'
    assert directives[0].actor == 'mikelear'


def test_run_gauntlet_honours_skip_check(tmp_path: Path) -> None:
    """`skip_checks=['mypy']` → mypy is skipped, others still run."""
    repo = _python_repo(tmp_path)
    runner = _FakeRunner(
        outcomes={
            'ruff': _RunOutcome(returncode=0),
            'mypy': _RunOutcome(returncode=1, stderr='SHOULD NOT RUN'),
            'pytest': _RunOutcome(returncode=0),
        }
    )
    result = run_gauntlet(repo, skip_checks=['mypy'], runner=runner)
    assert result.passed is True
    skipped_names = {name for name, _ in result.skipped}
    assert 'mypy' in skipped_names
    # ruff (twice — format + check) and pytest ran; mypy did NOT.
    invoked = [c[0] for c in runner.calls]
    assert 'mypy' not in invoked
    assert 'pytest' in invoked


# ─── System prompt enforcement (the calibration goal) ────────────────────────


def test_prompt_step4_mentions_gauntlet_before_push() -> None:
    """Step 4 (initial PR push) must say `commit → run gauntlet → push if green`."""
    from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT

    # Find the Step 4 block.
    assert '4. **Commit → run gauntlet → push if green**' in INITIATIVE_SYSTEM_PROMPT


def test_prompt_step10_mentions_gauntlet_in_fix_code_and_fix_test_rows() -> None:
    """The classifier-action table (Step 10) must require gauntlet in fix_code + fix_test."""
    from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT

    # Both fix_code and fix_test rows must call out the gauntlet, not just "push".
    fix_code_line = [line for line in INITIATIVE_SYSTEM_PROMPT.splitlines() if '`fix_code`' in line]
    fix_test_line = [line for line in INITIATIVE_SYSTEM_PROMPT.splitlines() if '`fix_test`' in line]
    assert fix_code_line and 'gauntlet' in fix_code_line[0].lower()
    assert fix_test_line and 'gauntlet' in fix_test_line[0].lower()


def test_prompt_dedicated_section_describes_gauntlet() -> None:
    """A standalone `## Pre-push gauntlet` section must spell out the rules."""
    from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT

    assert '## Pre-push gauntlet' in INITIATIVE_SYSTEM_PROMPT
    # The four supported languages must be cited so the agent has a fallback
    # when `gate.agent.gauntlets` can't be imported.
    for lang in ('python', 'go', 'angular', 'rust'):
        assert f'**{lang}**' in INITIATIVE_SYSTEM_PROMPT.lower() or f'| **{lang}**' in INITIATIVE_SYSTEM_PROMPT.lower()


def test_prompt_calls_out_skip_gauntlet_human_only() -> None:
    """The `/skip-gauntlet` bypass must be flagged as HUMAN-only — the agent never posts it."""
    from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT

    assert '/skip-gauntlet' in INITIATIVE_SYSTEM_PROMPT
    # Find the paragraph that describes it.
    block = INITIATIVE_SYSTEM_PROMPT
    idx = block.index('/skip-gauntlet')
    # Within ~600 chars of the first mention we should see the human-only invariant.
    nearby = block[max(0, idx - 100) : idx + 600].lower()
    assert 'human' in nearby
    assert 'never' in nearby


def test_prompt_hard_rule_block_enforces_gauntlet() -> None:
    """Hard-rules section must include `Always run the pre-push gauntlet`."""
    from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT

    assert 'Always run the pre-push gauntlet' in INITIATIVE_SYSTEM_PROMPT


def test_prompt_says_e2e_cannot_run_locally() -> None:
    """Agent must NOT try to run scripts/e2e.sh as part of the gauntlet."""
    from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT

    # The explicit "don't try to run scripts/e2e.sh" instruction.
    assert 'scripts/e2e.sh' in INITIATIVE_SYSTEM_PROMPT
    # And the cannot-run-locally list must include end2end + image-scan.
    for cluster_gate in ('image-scan', 'dynamic-scan', 'ai-review', 'end2end'):
        assert cluster_gate in INITIATIVE_SYSTEM_PROMPT


# ─── Initial-PR-push vs iteration-push: ordering check ───────────────────────


def test_prompt_step4_precedes_step10_gauntlet_mention() -> None:
    """Step 4 mention must appear earlier in the prompt than the Step 10 table."""
    from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT

    step4_idx = INITIATIVE_SYSTEM_PROMPT.index('4. **Commit → run gauntlet → push if green**')
    table_idx = INITIATIVE_SYSTEM_PROMPT.index('`fix_code`')
    assert step4_idx < table_idx
