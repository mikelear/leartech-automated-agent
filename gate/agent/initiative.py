"""Async initiative-driver loop using the Claude Agent SDK in write mode.

Reads a YAML initiative, drives Claude through the full make-change → commit → push →
open-PR → watch-gate → iterate-on-failure cycle. Uses `query()` (single fire-and-forget
session) with a high `max_turns` budget — the agent's internal calling of
`run_criteria_set` is itself the iteration loop. If you need cross-session resumption
or interactive interrupts, swap to `ClaudeSDKClient` (v1.5).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from gate import identity, obslog
from gate.agent import agentrun_client, pr_handoff
from gate.agent.calibrations import load_jx3_calibration
from gate.agent.initiative_prompt import render_initiative_system_prompt
from gate.agent.main import DEFAULT_MODEL, MCP_ALLOWED_TOOLS
from gate.agent.tool_logging import log_tool_call, log_tool_result
from gate.initiatives import load_initiative
from gate.mcp_servers import build_remote_mcp_servers
from gate.mcp_servers.call import call_mcp_tool

logger = logging.getLogger(__name__)

INITIATIVE_TEKTON_TOOLS = [
    'mcp__leartech-tekton__list_pipelineruns_for_pr',
    'mcp__leartech-tekton__step_status',
    'mcp__leartech-tekton__step_logs',
    'mcp__leartech-tekton__cancel_pipelinerun',
    'mcp__leartech-tekton__cancel_superseded_for_pr',
    'mcp__leartech-tekton__wait_first_failure',
]


@dataclass(frozen=True)
class RunSummary:
    """Outcome of a single initiative run."""

    exit_code: int
    turns: int | None = None
    cost_usd: float | None = None
    pr_number: int | None = None


PR_NUMBER_HINT_FILE = '/tmp/run_pr_number'  # noqa: S108  # nosec B108


def _write_pr_number_hint(pr_number: int | None) -> None:
    """Best-effort: write the PR number to ``PR_NUMBER_HINT_FILE`` for the preStop hook.

    The Job pod's preStop lifecycle hook (D.7) reads this file via
    ``$(cat /tmp/run_pr_number)`` to populate the ``--pr`` flag of
    ``python -m gate.agent.crash_sticky``. Empty / missing file → hook
    skips the sticky post gracefully.

    Tolerates any OSError (read-only fs, missing /tmp, etc.) — the file
    is purely an enrichment for the cancel path; the agent loop is fine
    without it.
    """
    if pr_number is None:
        return
    try:
        with open(PR_NUMBER_HINT_FILE, 'w', encoding='utf-8') as fh:
            fh.write(str(pr_number))
    except OSError:
        pass


def _resolve_pr_number(qualified_repo: str, branch: str, *, state: str = 'open') -> int | None:
    """Best-effort BREAK-GLASS: ask GitHub for a PR on `branch`. Returns None on miss/error.

    NOT the primary PR signal — that is ``AgentRun.status.targetPR`` (written by the
    ``open_pr`` MCP tool), read via ``_resolve_target_pr``. This ``gh`` query is only
    a fallback for the rare case where the authoritative status field is empty, and
    for resume-detection.

    ``state``:
      * ``'open'`` (default) — resume-detection semantics: only an OPEN PR is a
        resumable prior attempt.
      * ``'all'`` — end-of-run resolution semantics: a MERGED PR is a SUCCESS, so
        the fallback must see merged/closed too. ``--state open`` here was the
        root cause of the expected_pr_missing false-FAIL (a PR merged by Tide
        before run-end looked identical to "never created").

    Runs synchronously; called at most once at end-of-run so the few-hundred-ms cost
    is fine.

    Side effect (D.7): on resolution, writes the PR number to
    ``/tmp/run_pr_number`` so the spawned Job pod's preStop hook can post a
    "cancelled" sticky to the PR if the operator triggers cancel after this
    point. Failure to write the hint file does not affect the return value.
    """
    try:
        result = subprocess.run(
            [
                'gh',
                'pr',
                'list',
                '--repo',
                qualified_repo,
                '--head',
                branch,
                '--state',
                state,
                '--json',
                'number',
                '--limit',
                '1',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        rows = json.loads(result.stdout or '[]')
        if not rows:
            return None
        number = rows[0].get('number')
        if number is None:
            return None
        resolved = int(number)
        _write_pr_number_hint(resolved)
        return resolved
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None


async def _resolve_target_pr(qualified_repo: str, branch: str) -> int | None:
    """Resolve the PR this run produced — AUTHORITATIVE (``status.targetPR``) first.

    This is the single PR-identity read for the end-of-run verdict/fail-fast. It
    exists because the same logical fact ("did this run produce a PR?") used to be
    re-derived from a flaky ``gh pr list --state open`` query that could not tell a
    MERGED PR (a success — Tide merged it) from a never-created one — the
    expected_pr_missing false-FAIL. The fix: read the value the ``open_pr`` MCP tool
    already wrote, in the SAME channel it was set, and only fall back to ``gh`` for
    the rare case where the authoritative field is empty.

    Resolution order (each logged to Loki as event="target_pr_resolved" with the
    ``source``, so every resolution's provenance is greppable):
      1. ``AgentRun.status.targetPR`` — written by ``open_pr`` (MCP). State-agnostic:
         set once and never cleared, so it SURVIVES the merge. This is the truth.
      2. ``gh pr list --state all`` (``_resolve_pr_number``) — break-glass for an
         empty status field (open_pr's publish didn't land, or a raw-``gh`` PR).
         Merge-aware, unlike the old ``--state open`` path.

    Never raises: a status-read failure degrades to the ``gh`` fallback, and a
    fallback failure yields ``None`` (a genuine "no PR" the fail-fast then catches).

    Reads identity via :mod:`gate.identity` so this resolver keeps working
    against the CAPTURED handle after :func:`run_initiative` strips those vars
    from :data:`os.environ` — the strip is for subprocesses, not for the
    in-process code that legitimately needs the handle.
    """
    run_name = identity.get_run_name()
    namespace = identity.get_namespace()
    status_enabled = identity.is_status_enabled()

    if run_name and namespace and status_enabled:
        current = await agentrun_client.get_target_pr(run_name, namespace)
        if current:
            try:
                number = int(current)
            except ValueError:
                number = None
            if number is not None:
                _write_pr_number_hint(number)
                obslog.emit(
                    'INFO',
                    'target_pr_resolved',
                    'resolved PR from AgentRun.status.targetPR (authoritative, open_pr-written)',
                    logger='agent.initiative',
                    run_id=run_name or None,
                    repo=qualified_repo,
                    branch=branch,
                    targetPR=number,
                    source='status',
                )
                return number

    number = _resolve_pr_number(qualified_repo, branch, state='all')
    obslog.emit(
        'INFO',
        'target_pr_resolved',
        'resolved PR via gh fallback (status empty)' if number is not None else 'no PR resolved for branch',
        logger='agent.initiative',
        run_id=run_name or None,
        repo=qualified_repo,
        branch=branch,
        targetPR=number,
        source='gh_fallback' if number is not None else 'none',
    )
    return number


async def _backstop_target_pr(*, qualified_repo: str, branch: str, pr_number: int | None) -> None:
    """Runtime backstop: guarantee ``AgentRun.status.targetPR`` is set even when the
    LLM opens a PR via raw ``gh`` without calling the ``open_pr`` MCP tool.

    ``open_pr`` is the AUTHORITATIVE writer of ``status.targetPR`` (+headBranch),
    and the controller keys its stop-on-merge correlation off that field. When the
    agent skips the tool the field stays empty, the merge can't be correlated, and
    the agent overruns polling release status until its deadline (observed in prod).
    This end-of-run backstop patches the field from the resolved PR (``_resolve_target_pr``
    — here that means the ``gh`` fallback fired, since a set ``status.targetPR`` makes
    this a no-op) AND emits a loud, structured Loki signal so a future forensic /
    scrum-master agent can harvest the "open_pr skipped" cases.

    Fully best-effort: only runs as a real AgentRun; does nothing (no patch, no
    event) when open_pr already set the field or when no PR was resolved; and any
    failure here is swallowed so it never changes the run's exit code.

    Identity reads go through :mod:`gate.identity` — that means (a) a
    subprocess (e.g. this repo's own pytest suite invoked via the SDK's Bash
    tool) whose stripped env carries NO identity gets a clean skip via the
    fresh-module-state fallback, and (b) the parent agent process keeps the
    handle it captured at run entry. Fix #1 in the sanitise-subprocess-identity
    initiative — the "existing guard skips entirely" comment now genuinely
    holds for subprocesses instead of being a hoped-for property.
    """
    run_name = identity.get_run_name()
    namespace = identity.get_namespace()
    status_enabled = identity.is_status_enabled()
    if not run_name or not namespace or not status_enabled:
        return
    try:
        current = await agentrun_client.get_target_pr(run_name, namespace)
        if current:
            return
        if pr_number is None:
            return
        await agentrun_client.patch_target_pr(run_name, namespace, pr_number)
        try:
            obslog.emit(
                'WARN',
                'targetpr_backstop_fired',
                'runtime recovered targetPR — agent opened a PR without calling open_pr',
                logger='agent.initiative',
                run_id=run_name,
                repo=qualified_repo,
                branch=branch,
                targetPR=pr_number,
                reason='open_pr_not_called',
            )
        except Exception as exc:  # noqa: BLE001 — a failed signal must not disappear the backstop
            logger.warning(
                'targetPR backstop obslog signal failed (patch already applied): %s',
                exc,
            )
    except Exception as exc:  # noqa: BLE001 — backstop must never change the run's exit code
        logger.warning('targetPR backstop failed (non-fatal): %s', exc)


EXPECTED_PR_MISSING_EXIT_CODE = 1


def _fail_fast_if_expected_pr_missing(
    *,
    pr_expected: bool,
    pr_number: int | None,
    exit_code: int,
    qualified_repo: str,
    branch: str,
) -> int:
    """Deterministic fail-fast: a PR-backed step that finished with NO PR must fail.

    A dev/infra agent is a single LLM ``query()`` session that exits 0 unless it
    crashes or hits max-turns. In prod an az-infra register step's agent failed
    to push a PR (bot push-perms) yet exited 0 → the AgentRun false-Succeeded;
    the miss was only caught later by the controller's ``kind:pr`` step check.
    This catches the same case at the AGENT layer: when a PR was expected but the
    status-first ``_resolve_target_pr`` came back ``None`` (``status.targetPR`` empty
    AND the merge-aware ``gh`` fallback found no PR in ANY state) after the agent
    finished, force a non-zero exit so the AgentRun goes Failed (and K8s can retry)
    instead of silently Succeeding. Because the resolver is authoritative + merge-aware,
    a MERGED PR yields a number here (not ``None``), so a shipped-and-merged run no
    longer false-FAILs — the bug this guard previously caused via ``--state open``.

    Returns the (possibly forced) exit code so the caller threads it into
    ``RunSummary.exit_code`` AND the trailing ``run_end`` event — so ``run_end``
    reflects the failure too.

    Complements, does NOT duplicate, ``_backstop_target_pr``: the backstop fires
    only when a PR *does* exist on the branch (recovering ``status.targetPR``
    when the LLM skipped the ``open_pr`` tool); this fail-fast fires only when
    *no* PR exists and one was expected. The two are mutually exclusive by
    construction (``pr_number`` present vs. ``None``).

    Only acts on a run that would otherwise report SUCCESS (``exit_code == 0``):
    the whole point is to convert a false-Succeed into a Failed. A run that has
    already failed (exit 1/2 — SDK crash, max-turns, operator cancel) is left
    untouched; its exit code already reflects the failure and carries meaning
    (e.g. 2 → K8s retry on cap-hit), so re-stamping it would only lose signal.

    Defensive by design: only force-fails on a confident ``pr_number is None``
    when a PR was expected. Callers that could not confidently resolve the PR
    (e.g. a resolve error) should pass ``pr_expected=False`` or otherwise avoid
    the confident-None state rather than have this force-fail on ambiguity.
    """
    if not pr_expected:
        return exit_code
    if exit_code != 0:
        return exit_code
    if pr_number is not None:
        return exit_code
    run_name = identity.get_run_name() or None
    obslog.emit(
        'ERROR',
        'expected_pr_missing',
        'PR-backed step finished with no PR on its branch — failing the run so it does not false-Succeed',
        logger='agent.initiative',
        run_id=run_name,
        repo=qualified_repo,
        branch=branch,
        reason='no_pr_produced',
    )
    return EXPECTED_PR_MISSING_EXIT_CODE


def _build_crash_sticky_body(
    *,
    reason: str,
    turn_count: int,
    max_turns: int,
    cost: float | None,
    hint: str,
) -> str:
    """Render the crash-sticky markdown. The marker lets future tooling find it."""
    cost_str = f'${cost:.4f}' if cost is not None else 'unknown'
    return (
        '<!-- leartech-agent-run -->\n'
        f'## ⚠ Agent run did not complete\n\n'
        f'**Reason**: {reason}\n\n'
        f'**Turns**: {turn_count}/{max_turns}  •  **Cost so far**: {cost_str}\n\n'
        f'{hint}\n'
    )


def _post_crash_sticky(*, qualified_repo: str, pr_number: int | None, body: str) -> None:
    """Best-effort: post a crash sticky to the PR.

    Called only from the harness's exception branches when the agent never reached
    its own step-11 sticky. Tolerates every failure mode (no PR, no network, gh
    auth issue) — we're already in an error path, so any secondary failure here is
    logged to stderr and swallowed.
    """
    if pr_number is None:
        click.echo('  (crash sticky skipped: no open PR resolved for this branch)', err=True)
        return
    try:
        result = subprocess.run(
            [
                'gh',
                'pr',
                'comment',
                str(pr_number),
                '-R',
                qualified_repo,
                '--body',
                body,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            click.echo(
                f'  (crash sticky post failed: gh exit {result.returncode}: {result.stderr.strip()})',
                err=True,
            )
        else:
            click.echo(f'  → crash sticky posted to PR #{pr_number}', err=True)
    except (subprocess.TimeoutExpired, OSError) as exc:
        click.echo(f'  (crash sticky post errored: {exc})', err=True)


DEFAULT_INITIATIVE_MAX_TURNS = 300

WRITE_MODE_TOOLS = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash']

WAIT_FOR_TERMINAL_TOOL_SUFFIX = 'wait_for_terminal'
CHECKS_STATUS_ALL_PASSED = 'all_passed'


def _tool_name_is_wait_for_terminal(name: str) -> bool:
    """True iff a tool name is the fail-fast full-terminal check.

    Matches the unqualified suffix rather than the fully-qualified
    ``mcp__leartech-jx3-flow__wait_for_terminal`` so a future MCP
    namespace/prefix rename doesn't quietly break the post-green
    hard-stop. ``wait_for_first_failure_or_all_pass`` is deliberately
    NOT matched — the in-loop fail-fast primitive can legitimately
    return ``all_passed`` mid-loop before the final-pass verification,
    so we only treat the FULL-terminal check as the completion signal.
    """
    return name.split('__')[-1] == WAIT_FOR_TERMINAL_TOOL_SUFFIX


def _all_passed_from_text(text: str) -> bool | None:
    """Read the tool's ``status`` field. None when the payload is not parseable JSON."""
    for candidate in (text, *text.split('\n')):
        stripped = candidate.strip()
        if not stripped.startswith('{'):
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get('status'), str):
            return bool(parsed['status'] == CHECKS_STATUS_ALL_PASSED)
    return None


def _tool_result_reports_all_passed(block: ToolResultBlock) -> bool:
    """True iff a ``wait_for_terminal`` result reports ``status: all_passed``.

    leartech-mcp-servers returns ``{status: all_passed|some_failed|timeout, checks,
    clusters_observed, clusters_unobserved, coverage_note, merged}``, so the status
    field is compared EXACTLY. A substring search for the token is only a fallback for
    an unparseable payload, and it logs when it is used.

    This matters because the resulting flag has three consumers and two of them turn a
    failure into a success: the verdict gate stops recording "PR opened but never green"
    as a failure, and the exit-code normalisation downgrades exit 1/2 to 0 so Kubernetes
    does not retry. Only the early-stop consumer is additionally guarded by the agent
    having made no tool call that turn. A loose match here is therefore a false-success
    risk, which is why the exact field is preferred: `all_passed` is currently only ever
    a status VALUE on the Go side, but ``coverage_note`` is free text in the same payload
    and the shape is owned by another repo.
    """
    if block.is_error:
        return False
    content = block.content
    if content is None:
        return False
    if isinstance(content, str):
        text = content
    else:
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get('text')
                if isinstance(value, str):
                    parts.append(value)
        text = '\n'.join(parts)

    exact = _all_passed_from_text(text)
    if exact is not None:
        return exact
    matched = CHECKS_STATUS_ALL_PASSED in text
    logger.warning(
        'wait_for_terminal result was not parseable JSON; fell back to a substring match (matched=%s)',
        matched,
    )
    return matched


def _default_repo_root(repo_name: str) -> Path:
    """Resolve the consumer-repo checkout path. Honours these in order:

    1. `LEARTECH_REPO_ROOT` env var → treat as a parent directory; append `<repo-name>` to it.
       (Useful for cluster pods where checkouts live at `/workspace/<repo>`.)
    2. Fallback: `~/leartech/<repo-name>` — the laptop convention.
    """
    repo_short = repo_name.split('/')[-1]
    parent_override = os.environ.get('LEARTECH_REPO_ROOT')
    if parent_override:
        return Path(parent_override).expanduser() / repo_short
    return Path('~/leartech').expanduser() / repo_short


def _clone_repo(*, qualified_repo: str, cwd: Path) -> tuple[int, str | None]:
    """Clone `qualified_repo` (e.g. `mikelear/leartech-automated-agent`) into `cwd`.

    Uses direct `git clone` over HTTPS with the GH_TOKEN as a basic-auth user
    (`x-access-token:<token>@github.com/...`). This is GitHub's documented
    git-over-HTTPS auth format and — critically — hits NO GitHub API; just the
    git wire protocol. That makes the clone immune to the 5000pts/h GraphQL
    rate-limit bucket that `gh repo clone` consumed.

    Returns ``(exit_code, failure_reason)``. The failure reason is the
    Layer-1-classified one-liner (e.g. ``clone_failed: GH_TOKEN unset``)
    when ``exit_code != 0``; None on success. The caller writes it to
    ``initiative_runs.error`` via ``write_failure_reason``.

    The token is never logged: we redact it from any echoed stderr
    before surfacing AND before constructing the failure reason.
    """
    gh_token = os.environ.get('GH_TOKEN')
    if not gh_token:
        click.echo(
            f'Repo checkout not found at {cwd} and GH_TOKEN is not set — cannot clone. '
            f'Either clone manually (`git clone https://github.com/{qualified_repo}.git {cwd}`) '
            f'or set GH_TOKEN.',
            err=True,
        )
        return 2, f'clone_failed: GH_TOKEN unset, cannot clone {qualified_repo}'
    click.echo(
        click.style(f'→ cloning {qualified_repo} → {cwd}', fg='cyan'),
        err=True,
    )
    cwd.parent.mkdir(parents=True, exist_ok=True)
    url = f'https://x-access-token:{gh_token}@github.com/{qualified_repo}.git'
    result = subprocess.run(
        ['git', 'clone', '--depth', '1', url, str(cwd)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        redacted_stderr = result.stderr.replace(gh_token, '***REDACTED***')
        click.echo(
            f'Clone failed (exit {result.returncode}):\n{redacted_stderr}',
            err=True,
        )
        snippet_lines = [line for line in redacted_stderr.splitlines() if line.strip()]
        snippet = snippet_lines[-1] if snippet_lines else f'git exit {result.returncode}'
        if 'Repository not found' in redacted_stderr or 'not found' in redacted_stderr.lower():
            reason = f'clone_failed: Repository not found (likely missing bot collaborator) for {qualified_repo}'
        else:
            reason = f'clone_failed: {snippet[:160]}'
        return 2, reason
    return 0, None


@dataclass(frozen=True)
class ResumeContext:
    """Detected resume-mode signals for the current run.

    ``is_resume`` — True iff EITHER the remote branch exists OR an open
    PR exists on that branch. When False the caller stays on the
    fresh-start path.

    ``pr_number`` — the resolved open PR number when ``gh pr list``
    found one; None when only the branch signal fired (branch pushed
    but PR never opened, e.g. prior pod died between push and
    ``gh pr create``).

    ``branch_exists_on_remote`` — True iff ``git ls-remote`` reported
    the branch ref. Used by the caller to decide whether to attempt
    ``git fetch`` + ``git checkout`` (skipped when only the PR signal
    fired without a confirmed branch, which shouldn't happen but the
    guard is cheap).
    """

    is_resume: bool
    pr_number: int | None
    branch_exists_on_remote: bool


def _remote_branch_exists(*, qualified_repo: str, branch: str) -> bool:
    """Return True iff ``git ls-remote`` finds ``refs/heads/<branch>`` on origin.

    Uses HTTPS + GH_TOKEN auth (same shape as ``_clone_repo``) so the
    check works in cluster pods where the git remote isn't configured
    yet (we call this BEFORE cloning is guaranteed to have populated
    ``origin``). Hits no GitHub API — just the git wire protocol —
    which keeps us out of the 5000pts/h GraphQL bucket that operator
    ``gh`` usage shares.

    Returns False on ANY failure: missing GH_TOKEN, subprocess timeout,
    non-zero exit, unexpected empty output. A False result is
    semantically "we don't know — treat as fresh-start", which is the
    safe fallback the initiative spec calls for.

    The ``--exit-code`` flag makes git exit 2 when no matching ref is
    found (distinct from exit 0 with a matching row); we treat both
    non-zero and empty-stdout cases as "branch absent".
    """
    gh_token = os.environ.get('GH_TOKEN')
    if not gh_token:
        return False
    url = f'https://x-access-token:{gh_token}@github.com/{qualified_repo}.git'
    try:
        result = subprocess.run(
            ['git', 'ls-remote', '--exit-code', '--heads', url, branch],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _detect_resume_context(*, qualified_repo: str, branch: str) -> ResumeContext:
    """Deterministically decide whether this run is a retry that should RESUME.

    Two independent signals; either alone flips us into resume mode:

      1. ``gh pr list --head <branch>`` returns an open PR → a prior
         attempt already opened the PR.
      2. ``git ls-remote --heads origin <branch>`` finds the ref → a
         prior attempt pushed the branch.

    Both are best-effort with short timeouts. Any failure → the signal
    reads False (unknown), and if BOTH read False the caller stays on
    the fresh-start path. This is the "safety fallback" the initiative
    goal spec calls for: guard on detection outcomes, don't break
    existing runs.

    The two signals are checked independently rather than "PR ⇒ branch
    exists" because in the empirical B1 shape we've seen both:

      * Prior pod died between push and ``gh pr create`` → branch
        exists but no PR yet.
      * Prior pod opened the PR successfully but died before finishing
        the SDK loop → both signals fire (the common case).

    Returning ``pr_number is not None`` lets the caller tell the LLM
    the exact PR number to reuse.
    """
    pr_number = _resolve_pr_number(qualified_repo, branch)
    branch_exists = _remote_branch_exists(qualified_repo=qualified_repo, branch=branch)
    is_resume = pr_number is not None or branch_exists
    return ResumeContext(
        is_resume=is_resume,
        pr_number=pr_number,
        branch_exists_on_remote=branch_exists,
    )


def _fetch_and_checkout_existing(*, cwd: Path, branch: str) -> bool:
    """Fetch the existing remote ``branch`` into ``cwd`` and check it out.

    Best-effort. Returns True on success; False on any subprocess
    failure (git not on PATH, timeout, non-zero exit, missing remote
    branch). The caller treats a False return as "resume fetch failed
    — fall back to fresh-start" so a stale-state edge case never
    wedges the run.

    Order matters: we ``fetch`` first (populates ``FETCH_HEAD`` + the
    remote-tracking ref) and only then ``checkout`` from the
    remote-tracking ref (creates a local branch tracking origin/<branch>).
    Using ``checkout -B`` because the local branch may or may not
    exist depending on how the clone was set up — ``-B`` handles both.
    """

    def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=str(cwd),
        )

    try:
        fetch = _run(
            ['git', 'fetch', 'origin', f'+refs/heads/{branch}:refs/remotes/origin/{branch}'],
            timeout=60,
        )
        if fetch.returncode != 0:
            click.echo(
                click.style(
                    f'  resume fetch failed (exit {fetch.returncode}): {fetch.stderr.strip()[:200]}',
                    fg='yellow',
                ),
                err=True,
            )
            return False
        checkout = _run(['git', 'checkout', '-B', branch, f'origin/{branch}'], timeout=30)
        if checkout.returncode != 0:
            click.echo(
                click.style(
                    f'  resume checkout failed (exit {checkout.returncode}): {checkout.stderr.strip()[:200]}',
                    fg='yellow',
                ),
                err=True,
            )
            return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        click.echo(click.style(f'  resume fetch/checkout errored: {exc}', fg='yellow'), err=True)
        return False
    return True


def _checkpoint_wip_on_crash(*, cwd: Path, branch: str) -> None:
    """Best-effort commit + push of the working tree when the SDK crashes.

    The SDK terminates abruptly mid-turn on cap-hit (#913), so whatever the agent
    did in its final turns (e.g. freshly-written test files) is left UNCOMMITTED
    and lost — resume-on-retry then continues from the agent's last self-push and
    redoes that chunk. This preserves it: push a checkpoint commit so resume picks
    up the TRUE latest work. Paired with the resume-fetch fix (resume is only as
    good as what got pushed). No-op on a clean tree; every failure is swallowed —
    we are already in the crash path and must not mask the original exception.
    """

    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60, cwd=str(cwd))

    try:
        if not _run(['git', 'status', '--porcelain']).stdout.strip():
            return
        _run(['git', 'add', '-A'])
        _run(['git', 'commit', '--no-verify', '-m', 'wip: turn-cap checkpoint (auto — resumable)'])
        push = _run(['git', 'push', 'origin', f'HEAD:{branch}'])
        ok = push.returncode == 0
        click.echo(
            click.style(
                f'  {"✓ pushed" if ok else "✗ could not push"} a WIP checkpoint of uncommitted '
                f'work to {branch} so resume continues from the latest'
                + ('' if ok else f' (push exit {push.returncode}: {push.stderr.strip()[:150]})'),
                fg='green' if ok else 'yellow',
            ),
            err=True,
        )
    except Exception as exc:  # noqa: BLE001 — crash-path best-effort; never re-raise
        click.echo(click.style(f'  WIP checkpoint errored (non-fatal): {exc}', fg='yellow'), err=True)


def _build_resume_preamble(*, branch: str, base: str, pr_number: int | None) -> str:
    """Render the "RESUME MODE" block prepended to the user prompt.

    The preamble tells the LLM three things it needs to know to avoid
    redoing work:

      1. This is a retry — the earlier pod pushed to ``<branch>``.
      2. The branch is ALREADY checked out (the harness ran ``git
         fetch`` + ``git checkout`` before the SDK loop started).
      3. If a PR is open (``pr_number`` is not None): DO NOT open a
         duplicate. If none is open yet: proceed to open one if the
         work is ready.

    The preamble is prescriptive — it tells the LLM what state the
    working tree is in, then defers to the system prompt's loop for
    the rest. We keep it short (≤ ~1000 tokens) so it doesn't crowd
    the context window; the durable behavioural nudges live in the
    system prompt / calibrations, not here.
    """
    pr_line: str
    if pr_number is not None:
        pr_line = (
            f'An open PR already exists on this branch: **#{pr_number}** '
            f'(it may have been opened by a prior pod OR by an Infra step). '
            f'CALL the `open_pr` MCP tool anyway — it is IDEMPOTENT: it ADOPTS '
            f'the existing PR (no duplicate) AND records {{targetPR, headBranch}} '
            f'onto THIS run so the step advances to AwaitingReview. Then reuse '
            f'that PR for commits + comments. Never run `gh pr create`.'
        )
    else:
        pr_line = (
            'No open PR is on this branch yet — a prior attempt pushed the '
            'branch but never opened one. When the work is ready CALL the '
            '`open_pr` MCP tool (it creates + records the PR); do NOT first '
            're-do the pushed commits, and never run `gh pr create`.'
        )
    return (
        '⚠ **RESUME MODE — this is a retry pod for an initiative whose '
        f'earlier pod pushed work to remote branch `{branch}`.**\n\n'
        f'The harness has already run `git fetch origin {branch}` + '
        f'`git checkout -B {branch} origin/{branch}` before invoking '
        "you, so your local working tree is on the prior attempt's "
        'HEAD. Do NOT re-create the branch from `'
        f'{base}` or attempt a fresh checkout.\n\n'
        f'{pr_line}\n\n'
        'Before you make any new changes, inspect the current state:\n'
        '  * `git log --oneline origin/' + base + '..HEAD` to see what '
        'commits the prior attempt already pushed.\n'
        '  * `git status` / `git diff` to confirm no uncommitted work '
        'is present.\n\n'
        'Then continue the initiative loop from wherever the prior '
        'attempt left off — likely at the "watch the gate + iterate '
        'on failures" step, since the branch + PR (if any) are already '
        'in place. Only make NEW commits if the existing work is '
        "incomplete for the initiative's goal. This preamble is a "
        'one-shot signal from the harness; the standard loop in the '
        'system prompt otherwise applies unchanged.'
    )


_AGENT_RUNTIME_REPO_SUFFIX = 'leartech-automated-agent'

_self_referential_signpost_emitted: bool = False


def _emit_self_referential_repo_signpost_if_applicable(initiative: object) -> None:
    """Emit ``self_referential_repo`` WARN once when the target repo is THIS repo.

    Fix #2 in sanitise-subprocess-identity. This is the one repo whose test
    suite implements — and therefore exercises — the code that patches
    AgentRun status. Anyone debugging a "run status looks wrong" symptom
    on this repo needs to know that a stray k8s write inside a test IS a
    hazard even when the code looks innocent. The line carries:

    - ``event="self_referential_repo"`` (stable, greppable) so a single
      Loki query finds every such run;
    - the ambient run fields (``run_id`` + ``namespace``) via obslog's own
      ``_context()`` so operators can pivot from the signpost to the run;
    - a message spelling out the diagnostic route (``managedFields`` →
      audit log → user-agent), including the critical caveat that
      ``kubectl logs`` is not sufficient because it dies with the pod.

    Not a running commentary — one line per process. Duplicates would
    drown the signal.
    """
    global _self_referential_signpost_emitted
    if _self_referential_signpost_emitted:
        return
    try:
        primary = getattr(initiative, 'primary', None)
        repo = str(getattr(primary, 'qualified_repo', '') or '').strip().lower()
    except Exception:  # noqa: BLE001 — signpost must never crash the run
        return
    if not repo or not repo.endswith(_AGENT_RUNTIME_REPO_SUFFIX):
        return
    _self_referential_signpost_emitted = True
    obslog.emit(
        'WARN',
        'self_referential_repo',
        (
            'This run targets leartech-automated-agent — the ONE repo whose '
            'test suite exercises code that patches AgentRun status. The '
            'AgentRun identity has been stripped from subprocess env '
            '(AGENT_RUN_NAME/NAMESPACE/STATUS), so a test running in a '
            'Bash-tool subprocess CANNOT reach the live AgentRun; if run '
            'status still looks wrong, that is a regression in this guard, '
            'not a test-suite bug. To diagnose: inspect '
            'AgentRun.metadata.managedFields to find the manager owning '
            'status.phase, then cross-reference with the GKE audit log '
            'for the ServiceAccount + user-agent that made the write. '
            '`kubectl logs` is NOT sufficient — the responsible pod dies '
            'with the run and its logs go with it.'
        ),
        logger='agent.initiative',
        repo=str(getattr(primary, 'qualified_repo', '') or ''),
        subprocess_env_stripped=True,
    )


async def run_initiative(
    initiative_path: Path,
    *,
    repo_root: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_INITIATIVE_MAX_TURNS,
) -> RunSummary:
    """Drive a single initiative end-to-end. Returns a summary of the run."""
    initiative = load_initiative(initiative_path)

    identity.capture_and_strip()

    _emit_self_referential_repo_signpost_if_applicable(initiative)

    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return RunSummary(exit_code=2)

    if initiative.is_multi_repo:
        click.echo(
            click.style(
                f'Initiative `{initiative.name}` declares {len(initiative.repos)} repos but '
                'multi-repo execution is not yet implemented.\n'
                'For now, split into one initiative per repo (each can still cite the same '
                'parent brief via `description:`). Multi-repo execution lands as a follow-up '
                'slice — see `project_next_phase_alignment.md`.',
                fg='red',
            ),
            err=True,
        )
        return RunSummary(exit_code=2)

    primary = initiative.primary
    cwd = repo_root or _default_repo_root(primary.qualified_repo)

    run_id_for_first_turn = identity.get_run_id() or None
    if not cwd.exists():
        clone_exit, clone_reason = _clone_repo(qualified_repo=primary.qualified_repo, cwd=cwd)
        if clone_exit != 0:
            return RunSummary(exit_code=clone_exit)

    blocks: list[str] = [load_jx3_calibration()]
    blocks.append(render_initiative_system_prompt(hold=initiative.hold))
    system_prompt = '\n\n---\n\n'.join(blocks)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={**build_remote_mcp_servers()},
        allowed_tools=[*WRITE_MODE_TOOLS, *MCP_ALLOWED_TOOLS, *INITIATIVE_TEKTON_TOOLS],
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
        cwd=str(cwd),
        add_dirs=[str(cwd)],
    )

    resume_context = _detect_resume_context(
        qualified_repo=primary.qualified_repo,
        branch=primary.branch,
    )
    resume_active = False
    if resume_context.is_resume:
        click.echo(
            click.style(
                f'→ resume detected: '
                f'branch_on_remote={resume_context.branch_exists_on_remote}, '
                f'pr={resume_context.pr_number}',
                fg='cyan',
            ),
            err=True,
        )
        if resume_context.branch_exists_on_remote:
            fetch_ok = _fetch_and_checkout_existing(cwd=cwd, branch=primary.branch)
            if fetch_ok:
                resume_active = True
                if resume_context.pr_number is not None:
                    _write_pr_number_hint(resume_context.pr_number)
            else:
                click.echo(
                    click.style(
                        '  resume fetch/checkout failed — falling back to fresh-start behaviour',
                        fg='yellow',
                    ),
                    err=True,
                )
        else:
            click.echo(
                click.style(
                    '  resume signal fired without branch-on-remote — skipping fetch, falling back to fresh-start',
                    fg='yellow',
                ),
                err=True,
            )

    base_prompt = (
        f'Run this initiative end-to-end. Your working directory is `{cwd}`.\n\n'
        f'```yaml\n{initiative_path.read_text()}\n```\n\n'
        f'Begin by checking what state the branch is in (`git status`, `git branch --show-current`), '
        f'then proceed through the loop in your system prompt.'
    )
    if resume_active:
        resume_preamble = _build_resume_preamble(
            branch=primary.branch,
            base=primary.base,
            pr_number=resume_context.pr_number,
        )
    else:
        resume_preamble = ''

    prompt_parts: list[str] = []
    if resume_preamble:
        prompt_parts.append(resume_preamble)
    prompt_parts.append(base_prompt)
    user_prompt = '\n\n---\n\n'.join(prompt_parts)

    click.echo(click.style(f'→ initiative: {initiative.name}', fg='green', bold=True), err=True)
    click.echo(click.style(f'  repo: {primary.qualified_repo}  branch: {primary.branch}', fg='green'), err=True)
    click.echo(click.style(f'  cwd: {cwd}', fg='green'), err=True)
    click.echo('', err=True)

    open_pr_args = {
        'run_id': identity.get_run_id(),
        'namespace': identity.get_namespace(),
        'repo': primary.qualified_repo,
        'base': primary.base or 'main',
        'head': primary.branch,
        'title': initiative.name,
        'body': f'Automated changes for initiative `{initiative.name}`.',
    }

    exit_code = 0
    last_turn_count = 0
    last_cost: float | None = None
    crash_sticky_body: str | None = None

    handoff_state: dict[str, int] = {'turn': 0, 'pr': 0}

    async def _handoff_checkpoint(*, turns: int, cost_usd: float | None, last_tool_call: str | None) -> None:
        """Write a durable checkpoint to the PR. Never raises — a failed
        checkpoint must not affect the run."""
        try:
            if handoff_state['pr'] <= 0:
                raw = await agentrun_client.get_target_pr(identity.get_run_name(), identity.get_namespace())
                if raw and str(raw).strip().lstrip('#').isdigit():
                    handoff_state['pr'] = int(str(raw).strip().lstrip('#'))
            if handoff_state['pr'] <= 0:
                return
            await pr_handoff.post_handoff(
                base_url=os.environ.get('LEARTECH_MCP_URL', '').rstrip('/'),
                repo=primary.qualified_repo,
                pr_number=handoff_state['pr'],
                run_id=run_id_for_first_turn,
                iteration=0,
                turns=turns,
                max_turns=max_turns,
                cost_usd=cost_usd,
                last_tool_call=last_tool_call,
                tool_caller=call_mcp_tool,
            )
        except Exception as exc:  # noqa: BLE001 — observability must not break the run
            logger.warning('PR checkpoint failed at turn %s: %s', turns, exc)

    exit_via_cancel = False
    current_turn_last_tool: str | None = None
    wait_for_terminal_tool_ids: set[str] = set()
    tool_names_by_id: dict[str, str] = {}
    terminal_all_passed_seen = False
    turn_made_tool_call = False
    pr_emitted: int | None = resume_context.pr_number if resume_active else None

    try:
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        click.echo(block.text)
                    elif isinstance(block, ToolUseBlock):
                        click.echo(click.style(f'\n→ {block.name}', fg='cyan'), err=True)
                        tool_names_by_id[block.id] = block.name
                        log_tool_call(block.name, block.input)
                        turn_made_tool_call = True
                        if _tool_name_is_wait_for_terminal(block.name):
                            wait_for_terminal_tool_ids.add(block.id)
                        current_turn_last_tool = block.name
                    elif isinstance(block, ThinkingBlock):
                        pass
            elif isinstance(message, UserMessage):
                content = message.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            log_tool_result(
                                tool_names_by_id.get(block.tool_use_id),
                                block.content,
                                is_error=bool(getattr(block, 'is_error', False)),
                            )
                            if block.tool_use_id in wait_for_terminal_tool_ids and _tool_result_reports_all_passed(
                                block
                            ):
                                terminal_all_passed_seen = True
            elif isinstance(message, ResultMessage):
                last_turn_count = message.num_turns
                cost = message.total_cost_usd if message.total_cost_usd is not None else 0.0
                last_cost = cost
                if pr_handoff.should_post(
                    turns=last_turn_count,
                    max_turns=max_turns,
                    last_posted_turn=handoff_state['turn'],
                ):
                    handoff_state['turn'] = last_turn_count
                    asyncio.create_task(
                        _handoff_checkpoint(
                            turns=last_turn_count,
                            cost_usd=cost,
                            last_tool_call=current_turn_last_tool,
                        )
                    )
                current_turn_last_tool = None

                if terminal_all_passed_seen and not turn_made_tool_call:
                    click.echo(
                        click.style(
                            '\n  ✓ wait_for_terminal reported all_passed and the agent has '
                            'posted its final summary — initiative COMPLETE. Stopping the SDK '
                            'loop (Tide auto-merges on green; the controller stops the agent on '
                            'merge). Not waiting for the PR to merge.',
                            fg='green',
                            bold=True,
                        ),
                        err=True,
                    )
                    break
                turn_made_tool_call = False
    except Exception as exc:  # noqa: BLE001 — SDK raises bare Exception; we narrow via turn-count heuristic
        _checkpoint_wip_on_crash(cwd=cwd, branch=primary.branch)
        if last_turn_count >= max_turns:
            click.echo(
                click.style(
                    f'\n‼ Initiative hit the max_turns ceiling ({max_turns}). '
                    f'The SDK terminates abruptly on cap-hit (known: issue #913, '
                    f'lesson agent-sdk-crash-during-long-initiative). Substantive work is '
                    f'likely already pushed; re-fire is idempotent (agent detects the existing '
                    f'branch + PR). To give more headroom: re-run with `--max-turns 250`.',
                    fg='red',
                    bold=True,
                ),
                err=True,
            )
            exit_code = 2
            crash_sticky_body = _build_crash_sticky_body(
                reason=f'hit the `max_turns` ceiling ({max_turns}).',
                turn_count=last_turn_count,
                max_turns=max_turns,
                cost=last_cost,
                hint=(
                    "Substantive work is likely already pushed (this PR's commits). "
                    'Re-fire is idempotent — the agent detects the existing branch + PR. '
                    'For more headroom, re-run with `--max-turns 250`.'
                ),
            )
        else:
            click.echo(
                click.style(
                    f'\n‼ Unexpected SDK exception at turn {last_turn_count}/{max_turns}: {exc}',
                    fg='red',
                    bold=True,
                ),
                err=True,
            )
            exit_code = 1
            crash_sticky_body = _build_crash_sticky_body(
                reason=f'SDK crashed unexpectedly: `{exc}`',
                turn_count=last_turn_count,
                max_turns=max_turns,
                cost=last_cost,
                hint=(
                    "Substantive work may already be pushed (this PR's commits). "
                    'Re-fire is idempotent — the agent detects the existing branch + PR.'
                ),
            )

        if max_turns and last_turn_count >= max_turns:
            logger.warning('initiative terminal: agent_sdk_max_turns_exceeded at turn %s', last_turn_count)
        else:
            logger.warning('initiative terminal: agent_sdk_error: %s: %s', type(exc).__name__, exc)

    if pr_emitted is None:
        pr_emitted = await _resolve_target_pr(primary.qualified_repo, primary.branch)

    if (
        crash_sticky_body is None
        and not exit_via_cancel
        and exit_code == 0
        and pr_emitted is not None
        and not terminal_all_passed_seen
    ):
        click.echo(
            click.style(
                f'\n  ‼ verdict gate: PR #{pr_emitted} was opened but wait_for_terminal never '
                'reported all_passed — the agent did not reach green (blocked / unfinished). '
                'Recording exit_code 1 so the step recycles the agent; opening a PR is not success.',
                fg='red',
                bold=True,
            ),
            err=True,
        )
        obslog.emit(
            'ERROR',
            'initiative_verdict',
            'PR opened but wait_for_terminal never reported all_passed — recording blocked/unfinished as failure',
            logger='agent.initiative',
            verdict='blocked_or_unfinished',
            pr_number=pr_emitted,
            terminal_all_passed_seen=False,
            exit_code=1,
        )
        exit_code = 1

    if exit_code in (1, 2) and not exit_via_cancel and terminal_all_passed_seen:
        click.echo(
            click.style(
                f'\n  ✓ exit_code normalisation: wait_for_terminal reported all_passed '
                f'(PR #{pr_emitted}); downgrading exit_code {exit_code} → 0 so K8s does not retry '
                'a path that already shipped confirmed-green work. The crash sticky + warn logs '
                'above remain as the operator-visible signal.',
                fg='cyan',
            ),
            err=True,
        )
        exit_code = 0

    pr_number = pr_emitted
    if pr_number is None and exit_code == 0:
        logger.warning(
            'pr_link_missing: agent reported success but no open PR on %s@%s',
            primary.qualified_repo,
            primary.branch,
        )
    if crash_sticky_body is not None:
        _post_crash_sticky(
            qualified_repo=primary.qualified_repo,
            pr_number=pr_number,
            body=crash_sticky_body,
        )

    await _backstop_target_pr(qualified_repo=primary.qualified_repo, branch=primary.branch, pr_number=pr_number)

    exit_code = _fail_fast_if_expected_pr_missing(
        pr_expected=bool(open_pr_args),
        pr_number=pr_number,
        exit_code=exit_code,
        qualified_repo=primary.qualified_repo,
        branch=primary.branch,
    )

    return RunSummary(
        exit_code=exit_code,
        turns=last_turn_count or None,
        cost_usd=last_cost,
        pr_number=pr_number,
    )


@click.command()
@click.argument('initiative_path', type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    '--repo-root',
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help='Override the auto-derived `~/leartech/<repo>` checkout path.',
)
@click.option('--model', default=DEFAULT_MODEL, show_default=True, help='Claude model.')
@click.option(
    '--max-turns', default=DEFAULT_INITIATIVE_MAX_TURNS, type=int, show_default=True, help='Hard cap on agent turns.'
)
def main(initiative_path: Path, repo_root: Path | None, model: str, max_turns: int) -> None:
    """Run an initiative YAML end-to-end via the write-mode agent."""
    obslog.info(
        'run_start',
        'initiative run starting',
        logger='agent.initiative',
        model=model,
        initiative=str(initiative_path),
    )
    try:
        summary = asyncio.run(run_initiative(initiative_path, repo_root=repo_root, model=model, max_turns=max_turns))
    except Exception as exc:
        obslog.error(
            'run_end',
            f'initiative run crashed: {exc}',
            logger='agent.initiative',
            exit_code=1,
            reason='crashed',
            error=str(exc),
        )
        raise
    obslog.emit(
        'INFO' if summary.exit_code == 0 else 'ERROR',
        'run_end',
        'initiative run finished',
        logger='agent.initiative',
        exit_code=summary.exit_code,
        targetPR=summary.pr_number,
        turns=summary.turns,
        cost_usd=summary.cost_usd,
    )
    sys.exit(summary.exit_code)


if __name__ == '__main__':
    main()
