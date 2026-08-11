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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.db import dispose_engine as _dispose_engine
from app.db import init_engine as _init_engine
from app.db import is_db_enabled
from gate import obslog
from gate.agent import agentrun_client
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
from gate.agent.test_mode import (
    maybe_open_pr_args_for_initiative,
    parse_test_mode,
    run_test_mode,
)
from gate.agent.tool_logging import log_tool_call, log_tool_result
from gate.initiatives import load_initiative
from gate.mcp_servers import (
    build_agent_local_server,
    build_artifacts_server,
    build_criteria_server,
    build_remote_mcp_servers,
)
from gate.watcher.iteration_loop import format_feedback_payloads_for_prompt

logger = logging.getLogger(__name__)

# Phase G.2 — step-aware failure diagnosis tools wired ONLY into the
# initiative role (the read-only review_agent in `gate/agent/main.py` keeps
# the slimmer MCP_ALLOWED_TOOLS set). Catalog (`mcp_catalog.yaml`) is the
# source of truth for role→MCP wiring; this list mirrors the two MCPs
# that carry step-aware diagnosis:
#
#   * ``leartech-tekton`` — REMOTE (Go leartech-mcp-servers at
#     ``${LEARTECH_MCP_URL}/mcp/tekton``). Wired via ``REMOTE_MCPS`` in
#     ``gate.mcp_servers.remote``. Exposes the six kubectl-backed tools.
#   * ``leartech-agent-local`` — IN-PROCESS SDK
#     (``gate.mcp_servers.agent_local``). Carries the two tools that
#     depend on state inside the agent pod (LLM classifier heuristics +
#     git ops on the cloned workspace) and therefore cannot move remote.
INITIATIVE_TEKTON_TOOLS = [
    'mcp__leartech-tekton__list_pipelineruns_for_pr',
    'mcp__leartech-tekton__step_status',
    'mcp__leartech-tekton__step_logs',
    'mcp__leartech-tekton__cancel_pipelinerun',
    'mcp__leartech-tekton__cancel_superseded_for_pr',
    'mcp__leartech-tekton__wait_first_failure',
    'mcp__leartech-agent-local__classify_step_failure',
    'mcp__leartech-agent-local__rebase_branch_on_base',
]


@dataclass(frozen=True)
class RunSummary:
    """Outcome of a single initiative run — surfaced to API callers via app.state."""

    exit_code: int
    turns: int | None = None
    cost_usd: float | None = None
    pr_number: int | None = None


# Phase D.7 — file the preStop hook reads to learn the PR number on cancel.
# Written at end-of-run by ``_resolve_target_pr`` (from the authoritative
# ``status.targetPR`` when available, else the ``gh`` fallback) so the hook has a
# value regardless of when the operator triggers cancel. Path is process-local so
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
    """
    run_name = (os.environ.get('AGENT_RUN_NAME') or '').strip()
    namespace = (os.environ.get('AGENT_RUN_NAMESPACE') or '').strip()
    status_enabled = (os.environ.get('LEARTECH_AGENTRUN_STATUS') or '').strip().lower() in ('1', 'true', 'yes', 'on')

    # 1. Authoritative: the value open_pr (MCP) wrote onto the CRD status.
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

    # 2. Break-glass fallback: gh, merge-aware. (Step 2 replaces this with the
    #    pr_context ``resolve_pr`` MCP read so the resolver never touches GitHub.)
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
    """
    run_name = (os.environ.get('AGENT_RUN_NAME') or '').strip()
    namespace = (os.environ.get('AGENT_RUN_NAMESPACE') or '').strip()
    status_enabled = (os.environ.get('LEARTECH_AGENTRUN_STATUS') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    # Local/dev runs (no AgentRun identity or status reporting disabled) → skip entirely.
    if not run_name or not namespace or not status_enabled:
        return
    try:
        current = await agentrun_client.get_target_pr(run_name, namespace)
        if current:
            # open_pr did its job — the authoritative writer already published the PR.
            return
        if pr_number is None:
            # Agent legitimately opened no PR on this branch — nothing to record.
            return
        # Empty status + a PR resolved on the branch → the agent opened a PR without
        # calling open_pr. Recover the field so the controller can correlate the merge.
        await agentrun_client.patch_target_pr(run_name, namespace, pr_number)
        # LOUD, deliberate forensic signal: greppable in Loki as
        # event="targetpr_backstop_fired". A downstream forensic / scrum-master agent
        # harvests these to spot plans/agents that skip the open_pr tool (plan health).
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
    except Exception as exc:  # noqa: BLE001 — backstop must never change the run's exit code
        logger.warning('targetPR backstop failed (non-fatal): %s', exc)


# Distinct non-zero exit for the "expected a PR but none produced" fail-fast.
# There is no EXIT_* enum in this module today (failure exits are the bare
# ``1``/``2`` literals used throughout ``run_initiative``), so we reuse the
# generic failure convention: 1 = the agent's own work is incomplete/wrong,
# 2 = an environment/config precondition failure. A finished PR-backed run with
# no PR on its branch is the former.
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
        # apply/check-only initiative, BA run, or no PR was expected — nothing to do.
        return exit_code
    if exit_code != 0:
        # Already a failure (crash / max-turns / cancel) — the exit code already
        # reflects it and its specific value is meaningful. Don't re-stamp.
        return exit_code
    if pr_number is not None:
        # A PR exists on the branch — the PR-backed step did its job.
        return exit_code
    # PR expected + confidently no PR on the branch → fail the run.
    run_name = (os.environ.get('AGENT_RUN_NAME') or '').strip() or None
    # LOUD, deliberate signal: greppable in Loki as event="expected_pr_missing".
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


# 60 → 150 → 1000 → 200. Re-baselined 2026-05-26 after the Phase 1 cascade hit
# the Anthropic org-level rate limit (20M prompt bytes/hour on Sonnet 4.6).
# Observed actual usage: agent-base + py + ng finished in 30, 30-something,
# and 30 turns respectively (~$0.50-$1 each). Even substantial initiatives
# (catalog-fire-fallback at 134 turns, OAuth challenge at 107) stayed well
# under 200. The 1000 cap was a "safety net against rabbit-holes" — at our
# new Haiku-default + tighter quotas, 200 is a more honest safety net that
# also caps any runaway burn quickly. Override per-run via `--max-turns N`
# for genuinely larger initiatives.
#
# 2026-08-11: raised 200 → 300. A full from-scratch site build (re-scaffold +
# 5 pages + design system + SSG + JSON-LD/llms.txt + unit + e2e specs, then
# drive gates green) legitimately exceeds 200 — mortgagesourcing-website-visual
# hit the 200 cap mid-build at turn 201 (SDK crashes abruptly on cap-hit, #913)
# having pushed only incomplete work, so its PR gates were red for missing specs
# it hadn't reached yet. With the resume-fetch fix above a cap-hit is now
# survivable (retry resumes the pushed branch), but 300 gives a single run enough
# headroom to finish + drive gates for these larger website builds without a
# forced retry. Still a hard runaway ceiling; override per-run for bigger jobs.
DEFAULT_INITIATIVE_MAX_TURNS = 300

# Standard write-mode toolkit. Bash gives `git`, `gh`, `npm`, etc.; the rest are file ops.
WRITE_MODE_TOOLS = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash']

# Fail-fast terminal MCP whose `all_passed` result means "the agent's job is
# COMPLETE" (see the initiative system prompt, step 9/12). We match on the
# unqualified suffix so a namespace change (`mcp__leartech-jx3-flow__…`) doesn't
# silently disable the hard-stop safety net below.
WAIT_FOR_TERMINAL_TOOL_SUFFIX = 'wait_for_terminal'


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


def _tool_result_reports_all_passed(block: ToolResultBlock) -> bool:
    """Best-effort: True iff a `wait_for_terminal` tool result says `all_passed`.

    The MCP returns a structured ``{status: all_passed|some_failed|timeout, …}``
    payload, but the SDK surfaces ``ToolResultBlock.content`` as either a plain
    string or a list of content parts (dicts with a ``text`` field). We stringify
    whatever shape it is and look for the ``all_passed`` token — a false negative
    (we fail to spot it) simply means the LLM-driven stop in the prompt remains the
    sole lever, which is the safe fallback. A false positive is guarded downstream:
    the loop only acts on this AFTER a turn that made no further tool calls (the
    agent's own "I'm done" signal), so mis-reading an unrelated payload can't cut
    off in-flight work.
    """
    if block.is_error:
        return False
    content = block.content
    if content is None:
        return False
    if isinstance(content, str):
        text = content
    else:
        # List of content parts — join any string / {"text": …} entries.
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get('text')
                if isinstance(value, str):
                    parts.append(value)
        text = ' '.join(parts)
    return 'all_passed' in text


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
        # Explicit refspec so the remote-tracking ref origin/<branch> is created
        # locally. Plain `git fetch origin <branch>` only populates FETCH_HEAD, so
        # the checkout below (`-B <branch> origin/<branch>`) then fails with
        # "origin/<branch> is not a commit" — especially on the pod's shallow
        # clone, which has no origin/<branch> ref. That false-negative dropped
        # resume to fresh-start and made every retry redo the whole build from
        # main (observed: mortgagesourcing-website-visual PR #3, 2026-08-11 — a
        # cap-hit retry threw away 88%-done pushed work). The refspec fetches the
        # branch tip AND wires origin/<branch> so the checkout resumes on it.
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
            return  # clean tree — the agent already pushed everything
        _run(['git', 'add', '-A'])
        # --no-verify: skip hooks (a WIP checkpoint must not be blocked by a
        # lint/test pre-commit hook — the point is to preserve work, not gate it).
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


async def run_initiative(
    initiative_path: Path,
    *,
    repo_root: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_INITIATIVE_MAX_TURNS,
) -> RunSummary:
    """Drive a single initiative end-to-end. Returns a summary of the run."""
    initiative = load_initiative(initiative_path)

    # ── TEST-MODE short-circuit ────────────────────────────────────────────
    # A plan step may set ``initiative.testMode`` to skip the LLM/SDK loop
    # entirely and directly self-report a Succeeded/Failed phase (with
    # optional open_pr exercise). ONLY honored when the guard env
    # ``LEARTECH_AGENT_TEST_MODE_ALLOWED=true`` is set — otherwise the
    # directive is IGNORED and the agent runs normally. Placed BEFORE the
    # ANTHROPIC_API_KEY check because test-mode's whole point is to skip the
    # LLM: a test-mode run with no key should still succeed.
    inputs_for_test_mode: dict[str, object] = {}
    if initiative.test_mode is not None:
        inputs_for_test_mode['testMode'] = initiative.test_mode
    test_mode_spec = parse_test_mode(inputs_for_test_mode)
    if test_mode_spec is not None:
        # Build open_pr args for the PR-backed dev-agent step — the real
        # path opens a PR on ``initiative.primary`` (repo + head branch), so
        # test-mode does too. The MCP tool's own test-mode support returns
        # a synthetic PR and still patches ``AgentRun.status.targetPR``.
        pr_target = initiative.primary
        open_pr_args = maybe_open_pr_args_for_initiative(
            qualified_repo=pr_target.qualified_repo,
            base_branch=pr_target.base or 'main',
            head_branch=pr_target.branch,
            title=f'[test-mode] {initiative.name}',
            body=(
                'This PR was opened by the agent in TEST-MODE. The MCP tool '
                'produces a synthetic PR (real GitHub call is bypassed by '
                'leartech-mcp-servers) — the point is to exercise the '
                'PR-capture path end-to-end without doing real work.'
            ),
        )
        exit_code = await run_test_mode(test_mode_spec, open_pr_args=open_pr_args)
        return RunSummary(exit_code=exit_code)

    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return RunSummary(exit_code=2)

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
            'leartech-test-artifacts': build_artifacts_server(),
            'leartech-criteria': build_criteria_server(),
            # Two tools that couldn't move remote (LLM classifier heuristics +
            # git ops on the cloned workspace). See `gate/mcp_servers/agent_local.py`.
            'leartech-agent-local': build_agent_local_server(),
            # Authed remote MCPs (Streamable-HTTP over the network) —
            # leartech-pr-context for open_pr, leartech-tekton for the
            # step-aware Tekton inspection surface previously reimplemented
            # in-process, AND leartech-jx3-flow for the PR-check status
            # surface (list_pr_checks / wait_for_terminal /
            # wait_for_first_failure_or_all_pass) previously served by an
            # in-process `pipeline_server` shim. Empty dict when unconfigured
            # so the agent degrades cleanly rather than crashing. See
            # gate/mcp_servers/remote.py for the registry.
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

    # PR-backed step marker. ``run_initiative`` (this non-test dev-agent path) is
    # the entrypoint that genuinely OPENS a PR — the LLM assembles the ``open_pr``
    # MCP call itself, but the initiative-shaped arg dict is deterministic, so we
    # build it up front. Its truthiness is the single "a PR was expected" signal
    # consumed by the end-of-run fail-fast below (mirrors the test-mode path,
    # which builds the same dict via ``maybe_open_pr_args_for_initiative``).
    open_pr_args = maybe_open_pr_args_for_initiative(
        qualified_repo=primary.qualified_repo,
        base_branch=primary.base or 'main',
        head_branch=primary.branch,
        title=initiative.name,
        body=f'Automated changes for initiative `{initiative.name}`.',
    )

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
    # Post-green hard-stop safety net (fix-agent-exit-on-mcp-success). The
    # PRIMARY lever is the system prompt telling the LLM to STOP once
    # `wait_for_terminal` returns `all_passed`. This is the belt-and-braces
    # backstop for the known SDK behaviour (issue #913: the SDK doesn't
    # cleanly terminate, so a model that lingers past "done" would keep
    # burning idle turns — the "agent outlives merged PR" overrun). We track
    # the tool_use_ids of `wait_for_terminal` calls, flip
    # ``terminal_all_passed_seen`` when one returns `all_passed`, and — only
    # AFTER a subsequent turn that made NO tool call (the agent emitting its
    # final text summary, its own designated done-signal) — break the SDK
    # loop. Gating on the no-tool turn means we never cut off the "ready for
    # review" sticky (a tool call) or a some_failed→fix iteration.
    wait_for_terminal_tool_ids: set[str] = set()
    # tool_use_id → tool name, so a ToolResultBlock (which carries only the id)
    # can be logged against the tool that produced it. See log_tool_result below.
    tool_names_by_id: dict[str, str] = {}
    terminal_all_passed_seen = False
    turn_made_tool_call = False
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
    # bug). The authoritative PR number is recorded onto AgentRun.status by the
    # ``open_pr`` MCP tool at PR-open; ``_resolve_pr_number`` remains only as a
    # read-only orphan-PR classifier at end-of-run.
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
                        # Structured, redacted trajectory → Loki. The '→ name'
                        # echo above is for a human tailing the pod; this carries
                        # the actual command/input so operators aren't blind to
                        # WHAT ran (e.g. how a private gs:// artifact was read).
                        tool_names_by_id[block.id] = block.name
                        log_tool_call(block.name, block.input)
                        # Post-green hard-stop safety net — this turn made a
                        # tool call, so it is NOT the agent's final
                        # no-tool "I'm done" summary turn. Record the
                        # ``wait_for_terminal`` invocation's id so the
                        # matching ToolResultBlock (delivered on a later
                        # UserMessage) can be recognised as the terminal
                        # completion signal.
                        turn_made_tool_call = True
                        if _tool_name_is_wait_for_terminal(block.name):
                            wait_for_terminal_tool_ids.add(block.id)
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
            elif isinstance(message, UserMessage):
                # Post-green hard-stop safety net — tool RESULTS come back to
                # the model as a UserMessage carrying ToolResultBlock(s). When
                # a `wait_for_terminal` result reports `all_passed`, flip the
                # flag so the ResultMessage boundary can end the loop once the
                # agent emits its final no-tool summary turn.
                content = message.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            # Structured, redacted tool OUTPUT → Loki, paired with
                            # the tool name via the id map. Completes the trajectory
                            # so a failed/odd command shows its result, not just
                            # that it ran.
                            log_tool_result(
                                tool_names_by_id.get(block.tool_use_id),
                                block.content,
                                is_error=bool(getattr(block, 'is_error', False)),
                            )
                            if (
                                block.tool_use_id in wait_for_terminal_tool_ids
                                and _tool_result_reports_all_passed(block)
                            ):
                                terminal_all_passed_seen = True
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

                # Post-green hard-stop safety net
                # (fix-agent-exit-on-mcp-success). If `wait_for_terminal`
                # has reported `all_passed` AND this just-completed turn made
                # no tool call, the agent has emitted its final text summary —
                # its own designated done-signal — and the initiative is
                # COMPLETE. Break the SDK loop here rather than relying solely
                # on the model to stop taking turns. This is the belt to the
                # prompt's braces: it caps the "agent outlives merged PR"
                # idle overrun (SDK issue #913 means the session doesn't
                # cleanly terminate, so a lingering model keeps burning turns).
                # Gating on ``not turn_made_tool_call`` guarantees we never
                # cut off the "ready for review" sticky or a some_failed→fix
                # iteration — those turns make tool calls.
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
                    await record_decision(
                        run_id_for_first_turn,
                        'decision',
                        'post-green hard-stop: wait_for_terminal=all_passed + final no-tool '
                        'summary turn — ending SDK loop without waiting for merge',
                        payload={'turn_count': last_turn_count, 'reason': 'all_passed_terminal'},
                    )
                    break
                # Reset the per-turn tool-call marker for the NEXT turn. Placed
                # after both the cancel + hard-stop checks so each ResultMessage
                # boundary reflects exactly the turn that just closed.
                turn_made_tool_call = False
    except Exception as exc:  # noqa: BLE001 — SDK raises bare Exception; we narrow via turn-count heuristic
        # The SDK's `receive_messages()` raises a generic Exception when the consumer-set
        # `max_turns` is reached (see issue #913) AND for genuine transport errors. We use
        # the most recent ResultMessage's `num_turns` to distinguish: if we got close to
        # the cap, it's almost certainly a cap-hit; otherwise it's a real crash.
        # In either case the agent never reached its own step-11 sticky, so the harness
        # posts a crash sticky itself once we've resolved the PR number below.
        #
        # First: best-effort push a checkpoint of any UNCOMMITTED work. The SDK
        # crashes mid-turn, so the final chunk (the tests it was writing when PR #3
        # hit the cap) is otherwise lost and redone on retry. Preserve it so
        # resume-on-retry continues from the true latest.
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

    # ── Verdict gate + exit-code normalisation ──────────────────────────
    # Core principle (Mike 2026-08-05): opening a PR is NOT success —
    # reaching green is. The single authoritative "the work shipped" signal
    # is ``terminal_all_passed_seen`` (wait_for_terminal reported all_passed —
    # the prompt's mandated completion signal, initiative_prompt.py §Stopping
    # criteria), NOT "a PR exists". An agent that opens a PR then declares
    # itself BLOCKED, or never gets the wait command to report all-green, must
    # record a FAILURE so the step recycles the agent (its normal retry loop).
    # A false-Succeed here is the exact bug that let a blocked run (no webhook,
    # 0 checks fired) report success while nothing was built (setup-mcp-design,
    # 2026-08-05).
    #
    # Resolve the PR ONCE, up-front, for the gates + fail-fast below. The signal is
    # AUTHORITATIVE — ``AgentRun.status.targetPR`` (written by the open_pr MCP tool),
    # read via ``_resolve_target_pr`` — with a merge-aware ``gh`` break-glass fallback
    # only when that field is empty. We deliberately no longer (a) scrape PR URLs out
    # of tool-result prose (mis-captured cited PRs — the wrong-PR bug), nor (b) rely on
    # ``gh pr list --state open`` as the primary signal (it returned None once Tide
    # merged the PR — the expected_pr_missing false-FAIL, since a merged PR is a
    # SUCCESS). Reused as ``pr_number`` at end-of-run — no second resolution.
    if pr_emitted is None:
        pr_emitted = await _resolve_target_pr(primary.qualified_repo, primary.branch)

    # Gate 1 — blocked / never-green (natural-end path only). The agent stopped
    # of its own accord (no SDK crash: ``crash_sticky_body is None``; no
    # operator cancel) with ``exit_code == 0`` and a PR open, but
    # wait_for_terminal never reported all_passed. It either posted a "blocked"
    # summary or ran out of road on red/absent checks. Record FAILED so the run
    # recycles. The reasons the agent already posted (its sticky + summary) are
    # untouched — only the exit code changes ("quick exit AND still post the
    # reasons, but that is not success").
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
        await write_failure_reason(
            run_id_for_first_turn,
            f'pr_opened_without_green: PR #{pr_emitted} on {primary.qualified_repo}@{primary.branch} '
            'never reached wait_for_terminal=all_passed (agent blocked or checks never went green)',
        )
        await record_decision(
            run_id_for_first_turn,
            'decision',
            f'verdict gate: pr_opened_without_green pr_number={pr_emitted}, exit_code 0 → 1',
            payload={
                'verdict': 'blocked_or_unfinished',
                'pr_number': pr_emitted,
                'terminal_all_passed_seen': False,
                'pre_gate_exit_code': 0,
                'post_gate_exit_code': 1,
            },
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

    # Gate 2 — crash / max-turns normalisation. When the SDK crashed, hit the
    # max_turns ceiling, or returned is_error=True (``exit_code in (1, 2)``)
    # AFTER the work genuinely shipped, exit 0 so K8s doesn't retry a path
    # whose substantive work already landed — each retry hits the same SDK
    # regression and eventually trips BackoffLimitExceeded (canonical case: run
    # 59aefbd8f2d8, PR #111 merged cleanly while status=failed). The rescue is
    # gated on ``terminal_all_passed_seen`` — CONFIRMED GREEN — NOT merely "a PR
    # was opened". That was the flaw: a blocked/never-green run (PR open,
    # nothing built) was also rescued to success. Confirmed-green is the correct
    # proxy for "shipped"; PR-opened is not. Operator-cancel intent is preserved
    # (``not exit_via_cancel``). The crash sticky + warn logs still fire below;
    # only the exit code changes.
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
        await record_decision(
            run_id_for_first_turn,
            'decision',
            f'exit_code normalisation: confirmed_green pr_number={pr_emitted}, downgrading exit_code {exit_code} → 0',
            payload={
                'pre_normalisation_exit_code': exit_code,
                'normalised_exit_code': 0,
                'pr_number': pr_emitted,
                'reason': 'all_passed_before_failure',
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

    # Reuse the PR resolved once above (status-first, via ``_resolve_target_pr``) —
    # no second ``gh`` shellout, and no divergent representation of the same fact.
    # Between the verdict gate and here the LLM loop is finished, so no new PR can
    # appear; ``pr_emitted`` is the authoritative value.
    pr_number = pr_emitted
    # PR publish is owned by the open_pr MCP tool (it patches AgentRun.status
    # {targetPR, headBranch} at create time; the controller then emits the
    # Maestro run.pr_opened). The agent no longer patches/emits here — the
    # resolved value above is only the read-only signal for the orphan-PR
    # classification below + the exit-code normalisation + the fail-fast.
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

    # Runtime backstop: open_pr is the authoritative writer of
    # ``AgentRun.status.targetPR``, but if the LLM opened a PR via raw ``gh``
    # without calling the tool the field stays empty and the controller's
    # stop-on-merge correlation breaks (the agent then overruns polling release
    # status). This guarantees the field is set from the branch-scoped
    # ``_resolve_pr_number`` result above AND emits a loud, greppable Loki signal
    # (event="targetpr_backstop_fired") for downstream forensic harvesting. It is
    # a no-op when open_pr already set the field, when no PR was resolved, or when
    # not running as a real AgentRun — and any failure is swallowed.
    await _backstop_target_pr(qualified_repo=primary.qualified_repo, branch=primary.branch, pr_number=pr_number)

    # Deterministic fail-fast: a PR-backed step (``open_pr_args`` truthy) that
    # finished with NO PR on its branch must NOT false-Succeed. Force a non-zero
    # exit so the AgentRun goes Failed (and K8s can retry) and emit a loud,
    # greppable Loki signal (event="expected_pr_missing"). This is the AGENT-layer
    # complement to the controller's ``kind:pr`` step check. It is mutually
    # exclusive with the backstop above (that fires only when a PR exists; this
    # fires only when none does) and is a no-op when a PR was resolved or when no
    # PR was expected. Threaded into ``exit_code`` so ``run_end`` reflects it too.
    exit_code = _fail_fast_if_expected_pr_missing(
        pr_expected=bool(open_pr_args),
        pr_number=pr_number,
        exit_code=exit_code,
        qualified_repo=primary.qualified_repo,
        branch=primary.branch,
    )

    # Note: we used to emit a trailing `--- turns=... pr=N` stdout marker
    # here for a pod-log reconciler to grep. That consumer is long gone — the
    # authoritative ``AgentRun.status.targetPR`` is patched by the ``open_pr``
    # MCP tool, and the controller's maestro_producer emits ``run.pr_opened``
    # when it observes that change. The RunSummary below is the sole return
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
    # Phase-A observability: emit stable run-boundary events. obslog is
    # seam-agnostic — Phase B's runtime calls the same fns, so this survives the
    # refactor. run_end is THE authoritative per-run outcome line (one per run),
    # queryable in Loki: {namespace="jx-staging"} | json | event="run_end"
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
