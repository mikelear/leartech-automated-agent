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
from dataclasses import dataclass, field
from pathlib import Path

import click
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from app.db import dispose_engine as _dispose_engine
from app.db import init_engine as _init_engine
from app.db import is_db_enabled
from gate.agent.calibrations import load_jx3_calibration
from gate.agent.commands import (
    CommandSink,
    drain_commands,
    wait_while_paused,
)
from gate.agent.diagnostics import (
    ConversationBuffer,
    TerminateState,
    bump_turn_counter,
    classify_failure,
    install_terminate_handler,
    persist_conversation_snapshot,
    record_decision,
    uninstall_terminate_handler,
    write_failure_reason,
)
from gate.agent.initiative_prompt import render_initiative_system_prompt
from gate.agent.lessons import render_for
from gate.agent.main import DEFAULT_MODEL, MCP_ALLOWED_TOOLS
from gate.agent.run_driver import mark_first_turn, update_run_progress
from gate.initiatives import load_initiative
from gate.mcp_servers import (
    build_artifacts_server,
    build_criteria_server,
    build_pipeline_server,
    build_remote_mcp_servers,
    build_tekton_server,
)
from gate.watcher.iteration_loop import format_feedback_payloads_for_prompt

logger = logging.getLogger(__name__)

# Phase G.2 — step-aware failure diagnosis tools wired ONLY into the
# initiative role (the read-only review_agent in `gate/agent/main.py` keeps
# the slimmer MCP_ALLOWED_TOOLS set). Catalog (`mcp_catalog.yaml`) is the
# source of truth for role→MCP wiring; this list mirrors the
# `leartech-tekton` MCP's tool surface (see `gate/mcp_servers/tekton.py`).
INITIATIVE_TEKTON_TOOLS = [
    'mcp__leartech-tekton__list_pipelineruns_for_pr',
    'mcp__leartech-tekton__step_status',
    'mcp__leartech-tekton__step_logs',
    'mcp__leartech-tekton__cancel_pipelinerun',
    'mcp__leartech-tekton__cancel_superseded_for_pr',
    'mcp__leartech-tekton__wait_first_failure',
    'mcp__leartech-tekton__classify_step_failure',
    'mcp__leartech-tekton__rebase_branch_on_base',
]


@dataclass(frozen=True)
class RunSummary:
    """Outcome of a single initiative run — surfaced to API callers via app.state."""

    exit_code: int
    turns: int | None = None
    cost_usd: float | None = None
    pr_number: int | None = None


# Phase D.7 — file the preStop hook reads to learn the PR number on cancel.
# Updated mid-run by ``_resolve_pr_number`` so the hook has a current value
# regardless of when the operator triggers cancel. Path is process-local so
# absence on disk simply means "no PR yet" — the hook skips gracefully.
PR_NUMBER_HINT_FILE = '/tmp/run_pr_number'  # noqa: S108 — intentional service-internal tmp file


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
        # Non-fatal: the hint file is enrichment, not core flow.
        pass


def _resolve_pr_number(qualified_repo: str, branch: str) -> int | None:
    """Best-effort: ask GitHub for the open PR on `branch`. Returns None on miss/error.

    Runs synchronously; called once at end-of-run so the few-hundred-ms cost is fine.

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
                'open',
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


# 60 → 150 → 1000 → 200. Re-baselined 2026-05-26 after the Phase 1 cascade hit
# the Anthropic org-level rate limit (20M prompt bytes/hour on Sonnet 4.6).
# Observed actual usage: agent-base + py + ng finished in 30, 30-something,
# and 30 turns respectively (~$0.50-$1 each). Even substantial initiatives
# (catalog-fire-fallback at 134 turns, OAuth challenge at 107) stayed well
# under 200. The 1000 cap was a "safety net against rabbit-holes" — at our
# new Haiku-default + tighter quotas, 200 is a more honest safety net that
# also caps any runaway burn quickly. Override per-run via `--max-turns N`
# for genuinely larger initiatives.
DEFAULT_INITIATIVE_MAX_TURNS = 200

# Standard write-mode toolkit. Bash gives `git`, `gh`, `npm`, etc.; the rest are file ops.
WRITE_MODE_TOOLS = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash']


@dataclass
class LoopControlState:
    """Loop-side state for the bidirectional command queue.

    Owned by the run-driver; mutated by the :class:`LoopCommandSink`
    when the operator queues a command. The SDK loop reads
    ``cancel_requested`` at each turn boundary and ``paused`` whenever
    it would otherwise advance to the next SDK message.

    Why a mutable dataclass rather than passing flags through closures:

      The SDK loop runs as an ``async for`` over ``query()`` — it
      cannot be cleanly interrupted from outside. The natural
      injection point is at every ResultMessage (end of a model turn),
      where we drain the command queue and consult these flags. The
      dataclass gives the sink + the loop a shared, mutable record
      they can both observe.

    ``injected_guidance`` records text the operator wanted the model
    to see. In v1 we surface this via the conversation snapshot table
    (Layer 3 diagnostics) + decision log — operators can inspect what
    was injected post-run, and a v1.5 migration to
    :class:`claude_agent_sdk.ClaudeSDKClient` will deliver these into
    the model's input stream in real time. The CommandSink protocol
    means no wiring changes downstream when that lands.
    """

    cancel_requested: bool = False
    cancel_reason: str | None = None
    paused: bool = False
    injected_guidance: list[str] = field(default_factory=list)


@dataclass
class LoopCommandSink:
    """Concrete :class:`CommandSink` wired to the run-driver loop state.

    Holds references to the :class:`LoopControlState` and the
    :class:`ConversationBuffer` so each command handler can mutate the
    right surface. Construction is trivial — the loop builds one of
    these per run and passes it to :func:`drain_commands`.
    """

    state: LoopControlState
    buffer: ConversationBuffer

    def request_cancel(self, reason: str) -> None:
        # First cancel wins — operators may queue several
        # back-to-back; we keep the first reason so the failure
        # column attribution is stable.
        if not self.state.cancel_requested:
            self.state.cancel_requested = True
            self.state.cancel_reason = reason

    def set_pause(self, paused: bool) -> None:
        self.state.paused = paused

    def inject_user_message(self, text: str) -> None:
        # Two side-effects in one helper:
        #   1. Buffer the text for the loop to surface on the next
        #      poll (v1.5 will pump these into ClaudeSDKClient.query).
        #   2. Record the synthetic UserMessage in the conversation
        #      buffer so the snapshot table preserves what the
        #      operator said for post-run forensics.
        self.state.injected_guidance.append(text)
        self.buffer.append(
            {
                'role': 'user',
                'class': 'OperatorInjectedMessage',
                'content': text,
                'extras': {'source': 'inject_guidance'},
            }
        )


# Sanity check: ensure LoopCommandSink actually satisfies the Protocol
# at type-check time. mypy will catch a missing method here before any
# runtime invocation.
_loop_sink_satisfies_protocol: type[CommandSink] = LoopCommandSink


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
        # Defensive: redact the token from any error output before echoing.
        redacted_stderr = result.stderr.replace(gh_token, '***REDACTED***')
        click.echo(
            f'Clone failed (exit {result.returncode}):\n{redacted_stderr}',
            err=True,
        )
        # Surface a concise reason for the Layer-1 error column. Pick the
        # most informative line from stderr; default to the exit code
        # otherwise. "Repository not found" is the canonical
        # missing-collaborator shape called out in the initiative spec.
        snippet_lines = [line for line in redacted_stderr.splitlines() if line.strip()]
        snippet = snippet_lines[-1] if snippet_lines else f'git exit {result.returncode}'
        if 'Repository not found' in redacted_stderr or 'not found' in redacted_stderr.lower():
            reason = f'clone_failed: Repository not found (likely missing bot collaborator) for {qualified_repo}'
        else:
            reason = f'clone_failed: {snippet[:160]}'
        return 2, reason
    return 0, None


# ─── Resume-on-retry (reliability part 3) ────────────────────────────────
#
# When the agent Job pod dies (OOM, node preemption, SIGKILL from the
# cluster) and Kubernetes' backoffLimit spawns a retry pod, the retry
# pod would otherwise re-enter ``run_initiative`` and restart the
# initiative from scratch — fresh clone, redo every commit, potentially
# open a duplicate PR. Empirically motivated by run B1 (2026-07-13)
# where the first pod died and the retry redid ~all the work before the
# duplicate-PR path was blocked by GitHub's "PR already exists on branch"
# check.
#
# The durable work-state is the pushed git branch (``agent/<initiative>``)
# + any open PR on it. The retry pod needs to DETECT that state and
# RESUME from it rather than starting over. We do this deterministically
# by asking GitHub two independent questions:
#
#   1. Does an open PR exist on ``<qualified_repo> --head <branch>``?
#      (``gh pr list`` — same shape as ``_resolve_pr_number``.)
#   2. Does the remote branch itself exist? (``git ls-remote``.)
#
# Either signal alone flips us into resume mode. Both use short timeouts
# and swallow every subprocess/network failure — a detector that raised
# would break fresh-run behaviour, which is precisely what the initiative
# forbids.
#
# In resume mode the harness:
#   * ``git fetch``es the remote branch into the local clone,
#   * ``git checkout``s it (leaving HEAD on the prior attempt's work),
#   * writes the PR-number hint file so the preStop hook is armed even
#     before the SDK loop sees the PR URL,
#   * prepends a "RESUME MODE" preamble to the user prompt telling the
#     LLM this is a retry — DO NOT restart from scratch, DO NOT open a
#     duplicate PR, continue from the existing branch state.
#
# Safety: if any of detection / fetch / checkout fails, we fall back to
# the current fresh-start behaviour. The guard is on detection outcomes,
# not a global flag — a detection blip flips one specific run back to
# fresh-start without touching the rest. The system-prompt loop's own
# step 1 ("create from base if missing, checkout+pull if it exists")
# handles the fresh-start path, so a false-negative on resume-detection
# is at worst a "did the work over again" outcome (matching the pre-fix
# baseline behaviour) — never a corruption path.


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
        # Without auth we can only reach public repos; every consumer
        # repo we run against is private. Treat as "unknown → fresh".
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
    # exit 0 with non-empty output → branch exists. exit 2 (per git docs)
    # → no match. Anything else → error; treat as absent.
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
        fetch = _run(['git', 'fetch', 'origin', branch], timeout=60)
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
            f'An open PR already exists on this branch: **#{pr_number}**. '
            f'DO NOT open a duplicate PR — reuse this one for any '
            f'commits + comments. `gh pr create` would fail anyway '
            f'(GitHub rejects a second open PR on the same branch).'
        )
    else:
        pr_line = (
            'No open PR is on this branch yet — a prior attempt pushed '
            'the branch but never got as far as `gh pr create`. You '
            'MAY open the PR yourself when the work is ready; do NOT '
            'first re-do the pushed commits.'
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


async def run_initiative(
    initiative_path: Path,
    *,
    repo_root: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_INITIATIVE_MAX_TURNS,
) -> RunSummary:
    """Drive a single initiative end-to-end. Returns a summary of the run."""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return RunSummary(exit_code=2)

    initiative = load_initiative(initiative_path)

    # Multi-repo execution (coordinated changes across multiple PRs in a single
    # agent session) is a follow-up slice. Schema accepts the shape today; the
    # agent loop only handles len(repos) == 1 until that slice lands.
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

    # V5 D2.2 — the spawning router writes the run_id into the Job's env
    # so the agent loop can record the wall-clock moment its first SDK
    # turn fires. Unset on laptop runs (no router involved) → diagnostics
    # writes become no-ops, by design. We resolve this BEFORE the clone
    # so a clone failure can still attribute itself to the right run.
    run_id_for_first_turn = os.environ.get('LEARTECH_RUN_ID') or None
    db_engine_initialised = False
    if run_id_for_first_turn and is_db_enabled():
        _init_engine()
        db_engine_initialised = True

    if not cwd.exists():
        # Cluster mode: the consumer repo isn't pre-mounted, so clone it from GitHub
        # on demand. We use direct `git clone` over HTTPS (with GH_TOKEN injected
        # into the URL) rather than `gh repo clone`, because `gh` resolves the
        # clone URL via the GitHub GraphQL API — which shares a 5000pts/h bucket
        # with operator-side `gh` usage. Direct git protocol hits no API.
        # Laptop mode normally has the repo at ~/leartech/<repo>/ already, so this
        # branch only fires on a fresh dev machine or the deployed pod.
        clone_exit, clone_reason = _clone_repo(qualified_repo=primary.qualified_repo, cwd=cwd)
        if clone_exit != 0:
            # Layer 1 — Persist the classified clone failure to
            # ``initiative_runs.error`` before exiting so the operator
            # has the reason in the DB without pod-log archaeology.
            if clone_reason is not None:
                await write_failure_reason(run_id_for_first_turn, clone_reason)
            if db_engine_initialised:
                try:
                    await _dispose_engine()
                except Exception as exc:  # noqa: BLE001
                    click.echo(f'  (db engine dispose failed: {exc})', err=True)
            return RunSummary(exit_code=clone_exit)

    # Compose: JX3 platform calibration (static, shipped in wheel) → encoded
    # lesson calibrations (filtered by role) → initiative system prompt.
    # Both calibration sources stack on top of the role prompt; either or
    # both may be empty.
    #
    # ``hold`` is threaded through from the initiative YAML (default False) so
    # the role prompt tells the agent whether to post `/hold` after opening
    # the PR. Default False = let Tide auto-merge on green; the gate suite
    # (incl. ai-review) IS the review.
    blocks: list[str] = [load_jx3_calibration()]
    lessons = render_for('initiative_agent')
    if lessons:
        blocks.append(lessons)
    blocks.append(render_initiative_system_prompt(hold=initiative.hold))
    system_prompt = '\n\n---\n\n'.join(blocks)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={
            'leartech-pipeline': build_pipeline_server(),
            'leartech-test-artifacts': build_artifacts_server(),
            'leartech-criteria': build_criteria_server(),
            'leartech-tekton': build_tekton_server(),
            # Authed remote MCPs (Streamable-HTTP over the network) — currently
            # leartech-pr-context for open_pr. Empty dict when unconfigured, so
            # the agent degrades cleanly rather than crashing. See
            # gate/mcp_servers/remote.py.
            **build_remote_mcp_servers(),
        },
        allowed_tools=[*WRITE_MODE_TOOLS, *MCP_ALLOWED_TOOLS, *INITIATIVE_TEKTON_TOOLS],
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
        cwd=str(cwd),
        add_dirs=[str(cwd)],
    )

    # Reliability part 3 — resume-on-retry. When the agent Job pod dies
    # and K8s spawns a retry, the retry pod would otherwise restart the
    # initiative from scratch: fresh clone (done above), fresh branch,
    # redo every commit, potentially open a duplicate PR. We detect the
    # "prior attempt already pushed" state by querying GitHub for the
    # branch + open PR, and if either fires we ``git fetch`` +
    # ``git checkout`` the existing branch and prepend a "RESUME MODE"
    # preamble to the user prompt so the LLM knows to continue from the
    # pushed state instead of redoing the work.
    #
    # Safety: any failure — detection error, missing GH_TOKEN, fetch
    # timeout, checkout non-zero — flips us back to fresh-start (the
    # pre-fix baseline). We don't hard-fail the run on resume issues
    # because the system-prompt loop can recover from a fresh-start
    # against an already-existing remote branch anyway (its step 1
    # handles "checkout existing branch and pull").
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
                # Arm the preStop hook immediately: the PR number hint
                # file is normally written by ``_resolve_pr_number`` at
                # end-of-run, but on a resume we already know the PR
                # number BEFORE the SDK loop starts. A retry pod
                # cancelled early (before the run resolves the PR)
                # would otherwise not fire the crash sticky.
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
            # Only signal was the PR existing but no branch on remote —
            # that's a shape we don't expect (an open PR requires a
            # branch), so treat it as a detection blip and fall back.
            click.echo(
                click.style(
                    '  resume signal fired without branch-on-remote — skipping fetch, falling back to fresh-start',
                    fg='yellow',
                ),
                err=True,
            )

    # v6p0.5 step 2 — when the PR watcher re-spawned this run with prior-
    # attempt feedback, prepend the structured failure context BEFORE the
    # standard "Run this initiative end-to-end..." instruction. The order
    # matters: the agent must read the failure detail BEFORE starting its
    # standard loop (otherwise it begins from "branch check" and uses the
    # feedback as ambient context, which empirically gets de-prioritised
    # against the loop steps). ``format_feedback_payloads_for_prompt``
    # returns the empty string on a fresh first-attempt run, so this is
    # a no-op for runs that aren't re-spawns.
    feedback_block = format_feedback_payloads_for_prompt([dict(p) for p in initiative.feedback_payloads])
    base_prompt = (
        f'Run this initiative end-to-end. Your working directory is `{cwd}`.\n\n'
        f'```yaml\n{initiative_path.read_text()}\n```\n\n'
        f'Begin by checking what state the branch is in (`git status`, `git branch --show-current`), '
        f'then proceed through the loop in your system prompt.'
    )
    # Resume-on-retry — prepend the RESUME MODE block ahead of both the
    # respawn-feedback block (if any) and the base prompt. Order: resume
    # signal first (tells the LLM the working tree is on prior HEAD),
    # then per-attempt feedback (tells it what specifically failed in
    # the last cycle), then the standard loop kickoff. Empirically the
    # LLM prioritises the earliest-placed instructions when signals
    # conflict, so anchoring the physical-state message first prevents
    # the "start fresh" default from re-emerging.
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
    if feedback_block:
        prompt_parts.append(feedback_block)
    prompt_parts.append(base_prompt)
    user_prompt = '\n\n---\n\n'.join(prompt_parts)

    click.echo(click.style(f'→ initiative: {initiative.name}', fg='green', bold=True), err=True)
    click.echo(click.style(f'  repo: {primary.qualified_repo}  branch: {primary.branch}', fg='green'), err=True)
    click.echo(click.style(f'  cwd: {cwd}', fg='green'), err=True)
    click.echo('', err=True)

    # NOTE: ``run_id_for_first_turn`` + ``db_engine_initialised`` are
    # already resolved above the clone block — they need to be available
    # so a clone failure can attribute itself to the run via Layer 1.
    first_turn_recorded = False

    # Layer 2/3/4 — install the SIGTERM/atexit handler with a fresh
    # ``TerminateState`` keyed on this run. The handler reads from the
    # state at fire time so we can mutate ``last_turn_count`` /
    # ``buffer.messages`` throughout the loop and the snapshot is always
    # current.
    terminate_state = TerminateState(
        run_id=run_id_for_first_turn,
        max_turns=max_turns,
    )
    install_terminate_handler(terminate_state)
    conversation_buffer = terminate_state.buffer

    # Bidirectional command queue (initiative
    # agent-add-command-queue-with-injection). Operator-issued commands
    # land in ``agent_run_commands`` rows; the SDK loop drains them at
    # each turn boundary and applies the sink's primitives. DB-less
    # mode no-ops the drain — laptop runs see zero command-queue
    # overhead per turn.
    loop_state = LoopControlState()
    command_sink = LoopCommandSink(state=loop_state, buffer=conversation_buffer)

    async def _drain_then_check_cancel() -> bool:
        """Drain pending commands, returning True iff cancel was requested.

        Wraps the common turn-boundary work into one helper so the
        message-loop body stays readable. The cancel check happens
        AFTER the drain so a cancel queued in the same batch as
        other commands is observed in the same poll.
        """
        await drain_commands(run_id_for_first_turn, command_sink)
        if loop_state.paused and not loop_state.cancel_requested:
            click.echo(
                click.style(
                    '\n  ⏸ paused by operator command — waiting for resume',
                    fg='yellow',
                ),
                err=True,
            )
            await wait_while_paused(
                run_id_for_first_turn,
                command_sink,
                is_paused=lambda: loop_state.paused and not loop_state.cancel_requested,
            )
            click.echo(click.style('  ▶ resumed', fg='yellow'), err=True)
        return loop_state.cancel_requested

    exit_code = 0
    last_turn_count = 0
    last_cost: float | None = None
    crash_sticky_body: str | None = None
    # Initiative agent-fix-exit-code-after-pr-opened — distinguish the
    # operator-cancel path from the SDK-crash/max-turns path. The post-loop
    # normaliser preserves exit_code=2 when ``exit_via_cancel`` is set
    # (operator intent to terminate) but downgrades 1/2 → 0 when a PR was
    # opened before an SDK crash or max_turns hit (substantive work shipped;
    # re-firing is wasteful and triggers K8s BackoffLimitExceeded).
    exit_via_cancel = False
    # Per-turn writeback (initiative agent-add-per-turn-writeback).
    # We track the NAME of the last tool the agent invoked during the
    # in-flight turn so we can include it in the per-turn writeback
    # when the ResultMessage arrives. Reset to None at each
    # ResultMessage so a plain-text turn after a tool-using turn writes
    # NULL (the explicit "no tool this turn" signal). The "LAST tool
    # wins" rule means later ToolUseBlocks in a single AssistantMessage
    # overwrite earlier ones — operators see the most recent action.
    current_turn_last_tool: str | None = None
    # Reliability — resume-on-retry seed for the exit-code normalisation below
    # (which downgrades a 1/2 exit → 0 when a PR was opened during this run, so
    # K8s doesn't retry a crashed pod whose substantive work already shipped).
    # When this run is a retry pod that discovered an already-open PR on THIS
    # branch before the SDK loop started, seed ``pr_emitted`` with that number.
    # Source is AUTHORITATIVE + BRANCH-SCOPED ONLY: the resume-detection lookup
    # here (a branch-scoped ``gh pr list --head``), and a branch-scoped
    # ``_resolve_pr_number`` re-check at the exit-code path for non-resume runs.
    # We no longer scrape PR URLs out of tool-result prose — that matched
    # unrelated PRs the agent merely cited (the wrong-PR / targetPR mis-capture
    # bug). The authoritative PR number for the CR report-back is resolved
    # end-of-run via ``_resolve_pr_number`` + ``patch_pr_number``.
    pr_emitted: int | None = resume_context.pr_number if resume_active else None

    async def _record_first_turn_once() -> None:
        """V5 D2.2 hook — record `started_executing_at` on the first SDK message.

        Fires on the **first iteration of the SDK message loop**, before any
        message-type branching. The earlier wire-up gated this on the first
        ``AssistantMessage``; that's the agent's first *reply* but it isn't
        the only path through the loop's first iteration. A run that
        receives ``ResultMessage`` or ``SystemMessage`` first — even
        transiently — would fire the per-turn ``update_run_progress`` hook
        WITHOUT this writer having run first, leaving ``started_executing_at``
        NULL while ``turns`` / ``cost_usd`` advance. That's the production
        regression observed in run a9699b453342 (2026-06-12). Moving the
        call out of the AssistantMessage branch guarantees the writer fires
        before ANY per-turn writeback can.

        Idempotency is the contract — the in-process ``first_turn_recorded``
        flag short-circuits repeat calls and ``mark_first_turn``'s SQL guard
        (``WHERE started_executing_at IS NULL``) makes the DB write
        idempotent even across racing replicas.

        Tolerates every failure mode: missing run_id, no DSN, DB
        unreachable. The SDK loop is the primary mission and must not be
        aborted by an observability column failing to populate.

        Observability: logs at WARN with the run_id when the writer raises.
        Without this, a silently-failing writer (the original symptom of
        the bug above) leaves operators with no signal beyond the eventual
        NULL column — which is exactly how the regression went unnoticed
        for so long. Logging at WARN so a log-aggregation query
        (``level:WARN message:"started_executing_at"``) surfaces the next
        regression within seconds of it landing.
        """
        nonlocal first_turn_recorded
        if first_turn_recorded or not run_id_for_first_turn:
            return
        first_turn_recorded = True
        try:
            wrote = await mark_first_turn(run_id_for_first_turn)
        except Exception as exc:  # noqa: BLE001 — observability hook must not block the loop
            # WARN-level logger record so log-aggregation pipelines surface
            # the failure. The historical ``click.echo`` path went to stderr
            # but not through the logging level system, so structured queries
            # like ``level:WARN`` missed it. Keep an echo to stderr too —
            # it's the laptop-CLI human-readable signal — but ensure the
            # WARN record exists as the durable observability surface.
            logger.warning(
                'started_executing_at write failed for run %s: %s',
                run_id_for_first_turn,
                exc,
            )
            click.echo(
                click.style(
                    f'  (started_executing_at write failed for run {run_id_for_first_turn}: {exc})',
                    fg='yellow',
                ),
                err=True,
            )
            return
        if not wrote:
            # mark_first_turn returns False without raising when:
            #   - the in-memory record is absent AND the DB UPDATE matched
            #     0 rows (row missing, or column already non-NULL)
            #   - DB is disabled AND there's no in-memory record
            # On a real cluster run the row exists and the column is NULL,
            # so a False return here signals something is amiss
            # (e.g. wrong run_id env, schema drift, transient DB read of an
            # uncommitted row). Surface at WARN so the next regression
            # shows up in logs — historically this path was completely
            # silent.
            logger.warning(
                'started_executing_at first_turn write returned False for run %s '
                '(row missing, column already set, or DB disabled with no in-memory record)',
                run_id_for_first_turn,
            )
        else:
            logger.info(
                'started_executing_at first_turn write succeeded for run %s',
                run_id_for_first_turn,
            )

    try:
        async for message in query(prompt=user_prompt, options=options):
            # Layer 3 — buffer EVERY SDK message so the snapshot table can
            # be reconstructed on terminal (success, failure, or SIGTERM).
            # Normalisation happens inside ConversationBuffer.append.
            conversation_buffer.append(message)

            # V5 D2.2 — record ``started_executing_at`` BEFORE any
            # message-type branching. The earlier wire-up nested this
            # inside ``if isinstance(message, AssistantMessage):`` which
            # works for the common path (agent emits AssistantMessage
            # before ResultMessage) but leaves a regression hole if any
            # other message type arrives first. The per-turn
            # ``update_run_progress`` writeback fires on
            # ``ResultMessage`` — by hoisting this call above the
            # message-type checks we guarantee the first-turn timestamp
            # lands BEFORE any per-turn snapshot can write.
            # Idempotency is enforced by the ``first_turn_recorded`` flag
            # inside the helper, so calling it on every iteration is
            # effectively free after the first one.
            await _record_first_turn_once()

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        click.echo(block.text)
                    elif isinstance(block, ToolUseBlock):
                        click.echo(click.style(f'\n→ {block.name}', fg='cyan'), err=True)
                        # Per-turn writeback (initiative
                        # agent-add-per-turn-writeback). Track the
                        # LATEST tool the agent invoked this turn so the
                        # ResultMessage handler can flush it to
                        # ``initiative_runs.last_tool_call``. Last-wins
                        # by design — operators see the most recent
                        # action even when an AssistantMessage carries
                        # several ToolUseBlocks.
                        current_turn_last_tool = block.name
                        # Layer 2 — one decision row per tool invocation so
                        # the operator can read the agent's turn-by-turn
                        # trajectory from the DB. ``payload`` carries the
                        # tool input so the row is self-contained.
                        await record_decision(
                            run_id_for_first_turn,
                            'tool_call',
                            f'{block.name}',
                            payload={'tool': block.name, 'input': block.input},
                        )
                    elif isinstance(block, ThinkingBlock):
                        pass
            elif isinstance(message, ResultMessage):
                last_turn_count = message.num_turns
                terminate_state.last_turn_count = last_turn_count
                usage = message.usage or {}
                cost = message.total_cost_usd if message.total_cost_usd is not None else 0.0
                last_cost = cost
                # Per-turn writeback (initiative
                # agent-add-per-turn-writeback). Fire-and-forget via
                # ``asyncio.create_task`` so a slow DB round-trip never
                # stalls the next turn — the writeback is best-effort
                # observability, the SDK loop is the primary mission.
                # We pass the SDK's running ``num_turns`` + cumulative
                # ``total_cost_usd`` (NOT a delta) so the row always
                # reflects the latest snapshot. ``current_turn_last_tool``
                # is whatever the AssistantMessage handler last saw; we
                # reset it AFTER scheduling the task so the next turn
                # starts clean.
                asyncio.create_task(
                    update_run_progress(
                        run_id_for_first_turn,
                        turns=last_turn_count,
                        cost_usd=cost,
                        last_tool_call=current_turn_last_tool,
                    )
                )
                current_turn_last_tool = None
                # Layer 2 — bump the running turn counter + record a
                # 'turn_end' decision row so the operator's reconstruction
                # has natural turn boundaries even when no tool was called
                # in this turn.
                turn_idx = bump_turn_counter(run_id_for_first_turn) if run_id_for_first_turn else 0
                await record_decision(
                    run_id_for_first_turn,
                    'turn_end',
                    f'turn {turn_idx} ended (sdk num_turns={message.num_turns}, cost=${cost:.4f})',
                    payload={
                        'num_turns': message.num_turns,
                        'cost_usd': cost,
                        'is_error': message.is_error,
                        'usage': usage,
                    },
                    turn_index=turn_idx,
                )
                exit_code = 1 if message.is_error else 0

                # Bidirectional command queue — drain at the natural
                # turn boundary. The drain itself is sub-millisecond
                # when no commands are pending (partial index covers
                # the SELECT). When a cancel arrives, we break out of
                # the SDK iterator below so the agent shuts down
                # gracefully — Layer 1 diagnostics + the failure
                # column attribution flow through the existing
                # cancellation path.
                cancel_requested = await _drain_then_check_cancel()
                if cancel_requested:
                    reason = loop_state.cancel_reason or 'cancelled_by_operator: <no reason given>'
                    click.echo(
                        click.style(
                            f'\n  ✋ cancel requested by operator — exiting gracefully ({reason})',
                            fg='red',
                            bold=True,
                        ),
                        err=True,
                    )
                    # Layer 1 — surface the cancel reason in the
                    # error column. We use the well-known
                    # ``silent_terminate:`` prefix so the existing
                    # vocabulary catches it; the full reason text
                    # (including the operator-provided context) goes
                    # in the suffix per the
                    # ``<reason>: <context>`` format used everywhere
                    # else.
                    await write_failure_reason(
                        run_id_for_first_turn,
                        f'silent_terminate: {reason}',
                    )
                    await record_decision(
                        run_id_for_first_turn,
                        'terminate',
                        f'agent cancelled by operator command: {reason}',
                        payload={'reason': reason, 'turn_count': last_turn_count},
                    )
                    # Persist a snapshot while the DB connection is
                    # still hot — exit_code=2 below would otherwise
                    # fall through to the success-path snapshot
                    # writer, which is fine, but writing here gives
                    # us the explicit ``cancelled`` terminal_reason.
                    await persist_conversation_snapshot(
                        run_id_for_first_turn,
                        conversation_buffer,
                        terminal_reason='cancelled',
                    )
                    exit_code = 2
                    # Initiative agent-fix-exit-code-after-pr-opened — flag
                    # the cancel path so the post-loop normaliser does NOT
                    # downgrade this 2 → 0. Operator-intent-to-terminate
                    # must surface to the Job condition layer even if a PR
                    # was opened earlier in the run.
                    exit_via_cancel = True
                    break
    except Exception as exc:  # noqa: BLE001 — SDK raises bare Exception; we narrow via turn-count heuristic
        # The SDK's `receive_messages()` raises a generic Exception when the consumer-set
        # `max_turns` is reached (see issue #913) AND for genuine transport errors. We use
        # the most recent ResultMessage's `num_turns` to distinguish: if we got close to
        # the cap, it's almost certainly a cap-hit; otherwise it's a real crash.
        # In either case the agent never reached its own step-11 sticky, so the harness
        # posts a crash sticky itself once we've resolved the PR number below.
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

        # Layer 1 — Write the classified failure reason to
        # initiative_runs.error before exiting the exception branch.
        # ``classify_failure`` chooses the right vocabulary: cap-hit gets
        # ``agent_sdk_max_turns_exceeded``; everything else gets
        # ``agent_sdk_error: <ExcClass>: <message>``.
        reason = classify_failure(
            exc,
            last_turn_count=last_turn_count,
            max_turns=max_turns,
        )
        await write_failure_reason(run_id_for_first_turn, reason)
        # Layer 3 — Persist whatever conversation history is in-flight
        # at the moment of the crash. This is the only chance: the pod
        # may not reach the natural-terminal snapshot writer below.
        await persist_conversation_snapshot(
            run_id_for_first_turn,
            conversation_buffer,
            terminal_reason='failed',
        )
        # Layer 2 — Record the terminal decision so the operator's
        # ``SELECT ... FROM agent_run_decisions`` view captures the
        # moment of failure (not just the leading tool calls).
        await record_decision(
            run_id_for_first_turn,
            'terminate',
            f'agent terminated with exception: {reason}',
            payload={'reason': reason, 'turn_count': last_turn_count, 'max_turns': max_turns},
        )

    # Initiative agent-fix-exit-code-after-pr-opened — normalise the exit
    # code AFTER the SDK loop has settled but BEFORE the snapshot writer
    # picks ``terminal_reason``. When the substantive work (opening a PR)
    # completed before an SDK crash, max_turns hit, or is_error=True
    # ResultMessage, the process should exit 0 so K8s doesn't retry the
    # Job. Each retry hits the same SDK regression, runs to the same exit
    # point, costs another full agent cycle, and eventually trips
    # BackoffLimitExceeded — bogus failure marker for substantive work
    # that already shipped (canonical case: run 59aefbd8f2d8, PR #111
    # merged cleanly while agent_run.status=failed).
    #
    # The downgrade is gated on three conditions, in order:
    #   1. ``exit_code in (1, 2)`` — only failure exits get touched.
    #   2. ``not exit_via_cancel`` — operator-cancel intent is preserved
    #      (the operator deliberately triggered shutdown; surfacing that
    #      to the Job condition layer is correct).
    #   3. ``pr_emitted is not None`` — a PR was opened on THIS branch during
    #      the run. The signal is AUTHORITATIVE + BRANCH-SCOPED: the resume seed
    #      (branch-scoped resume detection) or, for non-resume runs, a
    #      branch-scoped ``_resolve_pr_number`` (gh pr list --head <branch>)
    #      re-check computed just below. We deliberately no longer scrape PR
    #      URLs out of tool-result prose — that matched unrelated PRs the agent
    #      merely cited and mis-set the number (the targetPR wrong-PR bug). The
    #      only downside vs the old scrape is the rare open-then-close-mid-loop
    #      race (the --head --state open query returns None), which at worst
    #      costs one wasteful K8s retry — never an incorrect result.
    #
    # The crash sticky still fires (set by the exception handler above,
    # posted by ``_post_crash_sticky`` further down) and the warn-level
    # logs still emit. Operators retain full visibility into the crash
    # path; only the process exit code changes.
    if pr_emitted is None and exit_code in (1, 2) and not exit_via_cancel:
        pr_emitted = _resolve_pr_number(primary.qualified_repo, primary.branch)
    if exit_code in (1, 2) and not exit_via_cancel and pr_emitted is not None:
        click.echo(
            click.style(
                f'\n  ✓ exit_code normalisation: PR #{pr_emitted} was opened during this run; '
                f'downgrading exit_code {exit_code} → 0 so K8s does not retry on a path that '
                'already shipped substantive work. The crash sticky + warn logs above remain '
                'as the operator-visible signal.',
                fg='cyan',
            ),
            err=True,
        )
        await record_decision(
            run_id_for_first_turn,
            'decision',
            f'exit_code normalisation: pr_opened pr_number={pr_emitted}, downgrading exit_code {exit_code} → 0',
            payload={
                'pre_normalisation_exit_code': exit_code,
                'normalised_exit_code': 0,
                'pr_number': pr_emitted,
                'reason': 'pr_opened_before_failure',
            },
        )
        exit_code = 0

    # Layer 3 — persist the full conversation snapshot on natural
    # terminal (success path). The exception branch above already
    # persisted on failure; this is the success-side companion. Doing it
    # BEFORE engine dispose so we still have a live connection pool.
    # The SIGTERM handler defers to this when we set
    # ``natural_terminal_completed`` below.
    if exit_code == 0:
        terminal_reason_snapshot = 'complete'
    else:
        terminal_reason_snapshot = 'failed'
    await persist_conversation_snapshot(
        run_id_for_first_turn,
        conversation_buffer,
        terminal_reason=terminal_reason_snapshot,
    )
    await record_decision(
        run_id_for_first_turn,
        'terminate',
        f'agent loop exited cleanly (exit_code={exit_code})',
        payload={
            'exit_code': exit_code,
            'turn_count': last_turn_count,
            'cost_usd': last_cost,
        },
    )
    # Tell the SIGTERM/atexit handler to back off — the natural-terminal
    # path has already written everything it would have flushed.
    terminate_state.natural_terminal_completed = True
    uninstall_terminate_handler()

    # V5 D2.2 — release the engine that ``mark_first_turn`` was sharing.
    # Paired with the eager init above so a long-running process doesn't
    # keep a connection pool alive past the SDK loop's lifetime. Tolerate
    # any disposal error — the agent is exiting anyway and the K8s Job
    # tears down the pod regardless.
    if db_engine_initialised:
        try:
            await _dispose_engine()
        except Exception as exc:  # noqa: BLE001 — disposal failure is non-fatal at end-of-run
            click.echo(
                click.style(
                    f'  (db engine dispose failed: {exc})',
                    fg='yellow',
                ),
                err=True,
            )

    pr_number = _resolve_pr_number(primary.qualified_repo, primary.branch)
    # PR publish is owned by the open_pr MCP tool (it patches AgentRun.status
    # {targetPR, headBranch} at create time; the controller then emits the
    # Maestro run.pr_opened). The agent no longer patches/emits here — the
    # branch-scoped _resolve_pr_number above is only the read-only signal for
    # the orphan-PR classification below + the exit-code normalisation.
    # Layer 1 + 2 — classify the orphan-PR case. When the agent reports
    # success but no PR exists on the branch, the operator needs to know.
    # The decision log captures the classification; the error column
    # carries the one-liner so a dashboard query surfaces it cheaply.
    if pr_number is None and exit_code == 0:
        orphan_reason = (
            f'pr_link_missing: agent reported success but no open PR on {primary.qualified_repo}@{primary.branch}'
        )
        await write_failure_reason(run_id_for_first_turn, orphan_reason)
        await record_decision(
            run_id_for_first_turn,
            'decision',
            'classified as pr_link_missing — success without observable PR',
            payload={'qualified_repo': primary.qualified_repo, 'branch': primary.branch},
        )
    elif pr_number is not None:
        await record_decision(
            run_id_for_first_turn,
            'decision',
            f'resolved PR #{pr_number} for {primary.qualified_repo}@{primary.branch}',
            payload={'pr_number': pr_number},
        )
    if crash_sticky_body is not None:
        _post_crash_sticky(
            qualified_repo=primary.qualified_repo,
            pr_number=pr_number,
            body=crash_sticky_body,
        )

    # Note: we used to emit a trailing `--- turns=... pr=N` stdout marker
    # here for a pod-log reconciler to grep. That consumer is long gone —
    # ``job_reconciler`` reads the authoritative ``AgentRun.status.targetPR``
    # (patched via :func:`patch_pr_number`) and the Maestro announce
    # (:func:`emit_run_pr_opened`). The RunSummary below is the sole return
    # channel; there is no stdout-side reporting.
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
    summary = asyncio.run(run_initiative(initiative_path, repo_root=repo_root, model=model, max_turns=max_turns))
    sys.exit(summary.exit_code)


if __name__ == '__main__':
    main()
