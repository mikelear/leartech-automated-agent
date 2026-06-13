"""Pre-push gauntlet — locally-runnable equivalents of the gate's PR pipelines.

The agent's iteration loop (``fix_code`` / ``fix_test`` on a gate failure)
historically pushed WITHOUT re-running the locally-available checks first.
Result: ``fix → push → wait ~10 min for Tekton → maybe still red →
re-iterate``.

That's wasteful when cheap checks (ruff, mypy, go-vet, eslint) catch the
same class of regression in seconds. The pre-push gauntlet promotes the
existing ``pre-push-validation`` calibration lesson from guidance to an
enforced action that runs BEFORE every ``git push`` — both the initial PR
push (Step 4) and every iteration push (Step 10) of the agent's loop.

## What the gauntlet is NOT

It is **not** a full reproduction of the cluster gate:

- ``image-scan``, ``dynamic-scan``, ``ai-review`` need cluster infrastructure.
- ``end2end`` / ``end2end-ui`` need a preview deploy.

Those stay on the Tekton side. The gauntlet's job is "fast-fail on the
locally-runnable subset" — typically lint + types + unit tests for the
detected language — so the agent doesn't burn a Tekton cycle on a typo.

## Structured contract

Failures surface as :class:`GauntletFailure` with the same payload shape
the iteration mechanism already consumes for Tekton failures:

    {
      "kind": "gauntlet_failure",
      "check": "<gauntlet-step-name>",
      "command": "<the literal shell command run>",
      "output": "<stderr/stdout tail>",
      "language": "<detected language>",
      "returncode": <int>,
    }

The watcher fingerprint + iteration-counter pieces from v6p0.6 step 3
treat ``gauntlet_failure`` identically to ``gate_failure`` /
``end2end_failure`` — same idempotency key shape, same dispatch path.

## Operator bypass

The agent itself MUST NEVER skip a gauntlet check. A human reviewer can
post::

    /skip-gauntlet ruff-check

as a PR comment and the next iteration will honour it (audited via
:func:`SkipGauntletDirective.actor` so the bypass is attributable). The
agent never posts ``/skip-gauntlet`` — same shape as the
``/skip-e2e-check`` and ``/hold cancel`` rules.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess  # nosec B404 — locally-runnable gate equivalents are exactly subprocess invocations
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


# ─── Per-language gauntlet definitions ──────────────────────────────────────


@dataclass(frozen=True)
class GauntletCheck:
    """One step in a language's pre-push gauntlet.

    ``command`` is split into argv via :func:`shlex.split` at run time so a
    missing toolchain is detected via :func:`shutil.which` on the first
    token. ``name`` is the stable identifier the operator uses with
    ``/skip-gauntlet <name>``.
    """

    name: str
    command: str

    @property
    def argv(self) -> list[str]:
        """The literal argv that :func:`subprocess.run` would invoke."""
        return shlex.split(self.command)

    @property
    def toolchain(self) -> str:
        """The first token — what :func:`shutil.which` checks for availability."""
        return self.argv[0] if self.argv else ''


# The canonical per-language sequence. Order matters: cheap checks first
# (lint format → lint check → types → unit tests). If lint fails, mypy
# and pytest are skipped — the agent has enough signal to iterate.
GAUNTLETS: Final[dict[str, tuple[GauntletCheck, ...]]] = {
    'python': (
        GauntletCheck(name='ruff-format', command='ruff format --check .'),
        GauntletCheck(name='ruff-check', command='ruff check .'),
        GauntletCheck(name='mypy', command='mypy .'),
        GauntletCheck(name='pytest', command='pytest -x -q'),
    ),
    'go': (
        GauntletCheck(name='go-fmt', command='gofmt -l .'),
        GauntletCheck(name='go-vet', command='go vet ./...'),
        GauntletCheck(name='golangci-lint', command='golangci-lint run'),
        GauntletCheck(name='go-test', command='go test -count=1 ./...'),
    ),
    'angular': (
        GauntletCheck(name='ng-lint', command='npm run lint'),
        GauntletCheck(name='ng-test', command='npm test -- --watch=false --browsers=ChromeHeadless'),
    ),
    'rust': (
        GauntletCheck(name='cargo-fmt', command='cargo fmt -- --check'),
        GauntletCheck(name='cargo-clippy', command='cargo clippy -- -D warnings'),
        GauntletCheck(name='cargo-test', command='cargo test'),
    ),
}


# Gates that REQUIRE cluster infrastructure (preview deploy / kaniko-built
# image / DAST) and so cannot be reproduced locally. The agent must not
# attempt to run these; they belong on the Tekton side.
SKIP_LOCALLY: Final[frozenset[str]] = frozenset(
    {
        'image-scan',
        'dynamic-scan',
        'ai-review',
        'end2end',
        'end2end-ui',
        'security-scan',  # SARIF generation needs cluster + scanner image
    }
)


# ─── Language detection ─────────────────────────────────────────────────────


_LANGUAGE_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    # Order matters: pyproject.toml + package.json could co-exist (Python
    # service shipping a frontend), but the gate file with PR builds is the
    # primary signal. We prioritise the build-system marker most likely to
    # be authoritative for the gate's pullrequest.yaml step.
    ('pyproject.toml', 'python'),
    ('go.mod', 'go'),
    ('Cargo.toml', 'rust'),
    ('angular.json', 'angular'),
    ('package.json', 'angular'),  # leartech Angular repos always ship angular.json too
)


def detect_language(repo_root: Path | str) -> str | None:
    """Inspect ``repo_root`` for a build-system marker, return the language key.

    Returns one of ``GAUNTLETS`` keys, or :data:`None` if no marker is
    recognised. The caller decides what to do with :data:`None` — typical
    response is "skip the gauntlet but warn", not "crash".
    """
    root = Path(repo_root)
    for marker, language in _LANGUAGE_MARKERS:
        if (root / marker).exists():
            return language
    return None


# ─── Structured failure payload ─────────────────────────────────────────────


@dataclass(frozen=True)
class GauntletFailure:
    """One step of the gauntlet that exited non-zero.

    Mirrors the contract of :class:`gate.tools.end2end_gate.End2EndFailure`
    and :class:`gate.tools.parsers.GateFailure` so the iteration watcher
    can treat all three uniformly.
    """

    check: str
    command: str
    output: str
    language: str
    returncode: int

    def to_dict(self) -> dict[str, object]:
        """Wire-format the failure for inclusion in feedback_payloads."""
        return {
            'kind': 'gauntlet_failure',
            'check': self.check,
            'command': self.command,
            'output': self.output,
            'language': self.language,
            'returncode': self.returncode,
        }

    def fingerprint(self) -> str:
        """Stable idempotency key — same shape as the other failure kinds.

        The iteration loop's ``already_handled_keys`` set rejects re-spawning
        on the identical failure twice; ``check + language`` is the right
        granularity (re-running the same gauntlet step is the same failure,
        even if the diff changed elsewhere).
        """
        return f'gauntlet:{self.language}:{self.check}'


@dataclass(frozen=True)
class GauntletResult:
    """Outcome of running a full gauntlet.

    - :attr:`passed`   — every available check exited 0 (or was skipped).
    - :attr:`failures` — checks that exited non-zero, in run order.
    - :attr:`skipped`  — checks that were skipped, paired with the reason
                        string (``missing-toolchain:<tool>`` or
                        ``operator-skip:<actor>``).
    """

    language: str
    passed: bool
    failures: tuple[GauntletFailure, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    ran: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            'language': self.language,
            'passed': self.passed,
            'ran': list(self.ran),
            'failures': [f.to_dict() for f in self.failures],
            'skipped': [{'check': name, 'reason': reason} for name, reason in self.skipped],
        }


# ─── Operator-bypass directive ──────────────────────────────────────────────


@dataclass(frozen=True)
class SkipGauntletDirective:
    """Parsed ``/skip-gauntlet <check>`` chatops directive.

    Audited so a future-self can prove who bypassed a check. The agent
    itself MUST NEVER emit this; ``actor`` is recorded for human-posted
    directives only.
    """

    check: str
    actor: str
    comment_id: int | None = None


_SKIP_DIRECTIVE_PREFIX: Final = '/skip-gauntlet'


def parse_skip_gauntlet_directives(
    comments: Iterable[dict[str, object]],
    *,
    agent_login: str | None = None,
) -> tuple[SkipGauntletDirective, ...]:
    """Extract ``/skip-gauntlet <check>`` directives from PR comments.

    ``comments`` is the shape returned by ``gh api .../comments``:
    each entry has ``body`` (str), ``user.login`` (str), and ``id`` (int).

    Comments posted by ``agent_login`` (when supplied) are rejected — the
    bypass is human-only. Same rule as ``/hold cancel`` and
    ``/skip-e2e-check``: the agent must not be able to silence its own
    checks.
    """
    out: list[SkipGauntletDirective] = []
    for c in comments:
        body = str(c.get('body', '') or '')
        user = c.get('user') or {}
        actor = ''
        if isinstance(user, dict):
            actor = str(user.get('login', '') or '')
        if agent_login is not None and actor and actor == agent_login:
            # Self-issued bypass is forbidden — audit-only path; never honoured.
            logger.warning('rejecting self-issued /skip-gauntlet from %s (agent login)', actor)
            continue
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith(_SKIP_DIRECTIVE_PREFIX):
                continue
            tail = stripped[len(_SKIP_DIRECTIVE_PREFIX) :].strip()
            if not tail:
                # `/skip-gauntlet` alone is too broad — require an explicit name.
                logger.warning('ignoring %r — missing check name', stripped)
                continue
            check_name = tail.split()[0]
            comment_id_raw = c.get('id')
            comment_id = int(comment_id_raw) if isinstance(comment_id_raw, int) else None
            out.append(
                SkipGauntletDirective(
                    check=check_name,
                    actor=actor or '<unknown>',
                    comment_id=comment_id,
                )
            )
    return tuple(out)


# ─── Pluggable subprocess runner (tests inject a fake) ──────────────────────


@dataclass(frozen=True)
class _RunOutcome:
    """In-process mirror of :class:`subprocess.CompletedProcess`."""

    returncode: int
    stdout: str = ''
    stderr: str = ''


class RunnerError(Exception):
    """Raised when the runner cannot even invoke the command (timeout, OS error)."""


class GauntletRunner:
    """Protocol-shaped base — tests can subclass with deterministic outcomes."""

    def which(self, tool: str) -> bool:  # pragma: no cover - protocol
        raise NotImplementedError

    def run(self, argv: Sequence[str], *, cwd: Path, timeout: int) -> _RunOutcome:  # pragma: no cover - protocol
        raise NotImplementedError


class _SubprocessRunner(GauntletRunner):
    """Real runner — thin wrapper over :func:`subprocess.run` + :func:`shutil.which`."""

    def which(self, tool: str) -> bool:
        return shutil.which(tool) is not None

    def run(self, argv: Sequence[str], *, cwd: Path, timeout: int) -> _RunOutcome:
        try:
            # nosec B603 — argv is sourced from a static GAUNTLETS dict, not user input
            completed = subprocess.run(  # noqa: S603
                list(argv),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f'gauntlet command {argv[0]!r} timed out after {timeout}s') from exc
        except OSError as exc:
            raise RunnerError(f'gauntlet command {argv[0]!r} failed to launch: {exc}') from exc
        return _RunOutcome(
            returncode=completed.returncode,
            stdout=completed.stdout or '',
            stderr=completed.stderr or '',
        )


# ─── Gauntlet runner ────────────────────────────────────────────────────────


# Default cap on per-step stderr/stdout we capture into the failure payload.
# The agent's prompt has a finite budget; 4 KB per failed step is generous
# but won't blow up a multi-failure rollup.
_DEFAULT_OUTPUT_TAIL_BYTES: Final = 4096


def _tail_bytes(text: str, limit: int = _DEFAULT_OUTPUT_TAIL_BYTES) -> str:
    """Keep the last ``limit`` bytes of ``text`` — failures cite the tail."""
    if len(text) <= limit:
        return text
    return '…[truncated]…\n' + text[-limit:]


def run_gauntlet(
    repo_root: Path | str,
    *,
    language: str | None = None,
    skip_checks: Sequence[str] = (),
    timeout_per_check: int = 600,
    runner: GauntletRunner | None = None,
) -> GauntletResult:
    """Execute the pre-push gauntlet against the repo at ``repo_root``.

    ``language`` overrides auto-detection when supplied (e.g. the initiative
    YAML explicitly named it). ``skip_checks`` is the list of check names
    the operator opted out of via ``/skip-gauntlet`` directives (already
    extracted by :func:`parse_skip_gauntlet_directives`).

    Missing toolchains (``command -v`` fails) are skipped with a warning,
    NOT a crash — the lesson is "fast-fail on what's locally runnable",
    not "demand a full local toolchain".

    Returns a :class:`GauntletResult` whose ``passed`` flag tells the caller
    whether to proceed with ``git push``.
    """
    root = Path(repo_root)
    detected = language or detect_language(root)
    if detected is None or detected not in GAUNTLETS:
        logger.info('no gauntlet defined for repo %s (language=%r)', root, detected)
        return GauntletResult(language=detected or 'unknown', passed=True)

    runner = runner or _SubprocessRunner()
    skip_set = {s.strip() for s in skip_checks if s.strip()}

    failures: list[GauntletFailure] = []
    skipped: list[tuple[str, str]] = []
    ran: list[str] = []

    for check in GAUNTLETS[detected]:
        if check.name in skip_set:
            skipped.append((check.name, f'operator-skip:{check.name}'))
            logger.info('skipping %s — operator bypass', check.name)
            continue
        if not runner.which(check.toolchain):
            skipped.append((check.name, f'missing-toolchain:{check.toolchain}'))
            logger.warning('skipping gauntlet check %s — %r not on PATH', check.name, check.toolchain)
            continue
        logger.info('running gauntlet check %s: %s', check.name, check.command)
        try:
            outcome = runner.run(check.argv, cwd=root, timeout=timeout_per_check)
        except RunnerError as exc:
            failures.append(
                GauntletFailure(
                    check=check.name,
                    command=check.command,
                    output=_tail_bytes(str(exc)),
                    language=detected,
                    returncode=-1,
                )
            )
            ran.append(check.name)
            # Don't keep running once anything failed — the agent has the
            # signal it needs to iterate; later checks may give noisy
            # secondary failures that obscure the root cause.
            break
        ran.append(check.name)
        # `gofmt -l` is the special case: exit 0 but non-empty stdout = failure.
        if check.name == 'go-fmt' and outcome.returncode == 0 and outcome.stdout.strip():
            failures.append(
                GauntletFailure(
                    check=check.name,
                    command=check.command,
                    output=_tail_bytes(outcome.stdout),
                    language=detected,
                    returncode=1,
                )
            )
            break
        if outcome.returncode != 0:
            tail_source = outcome.stderr or outcome.stdout
            failures.append(
                GauntletFailure(
                    check=check.name,
                    command=check.command,
                    output=_tail_bytes(tail_source),
                    language=detected,
                    returncode=outcome.returncode,
                )
            )
            break

    return GauntletResult(
        language=detected,
        passed=not failures,
        failures=tuple(failures),
        skipped=tuple(skipped),
        ran=tuple(ran),
    )


# ─── Public surface ─────────────────────────────────────────────────────────


__all__ = [
    'GAUNTLETS',
    'SKIP_LOCALLY',
    'GauntletCheck',
    'GauntletFailure',
    'GauntletResult',
    'GauntletRunner',
    'RunnerError',
    'SkipGauntletDirective',
    'detect_language',
    'parse_skip_gauntlet_directives',
    'run_gauntlet',
]
