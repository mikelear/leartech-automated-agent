"""leartech-jx3-flow-mcp — runtime "where is this PR in JX3" state queries.

Three tools:

- ``get_pr_jx3_stage`` — current stage + blocking predicate for one PR
- ``list_required_actions`` — chatops commands that would unblock the PR
- ``wait_for_merge`` — block until the PR is merged, fails, or times out

All three call into :mod:`gate.agent.jx3.rules` with a freshly-fetched
:class:`~gate.agent.jx3.rules.PRSnapshot`. The rules module is the
SINGLE SOURCE OF TRUTH; this module is just a thin GitHub-REST adapter
+ MCP wrapper.

Used by:
- the initiative-agent role (decide whether to keep waiting / retest / give up)
- the orchestrator role (poll a downstream PR until it lands or fails)

Authentication: requires ``GH_TOKEN`` in the environment. The token must
have ``pull_requests:read`` + ``checks:read`` on the target repo. In
production this is the same token wired by the auto-unhold + plan-runner
init-container chart work.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from gate.agent.jx3.rules import (
    CheckState,
    JX3Stage,
    PRSnapshot,
    blocking_predicate,
    classify_stage,
    required_actions,
)

GITHUB_API = 'https://api.github.com'
_REQUEST_TIMEOUT_S = 20.0
_DEFAULT_WAIT_TIMEOUT_S = 1800
_POLL_INTERVAL_S = 30.0


def _check_state_for(run: dict[str, Any]) -> CheckState:
    """Map a GitHub check-run object to our 4-value vocabulary.

    Mapping:
    - conclusion=success            → 'success'
    - conclusion in {failure, cancelled, timed_out, action_required}
                                    → 'failure'
    - status in {queued, in_progress, waiting}
                                    → 'pending'
    - everything else (neutral, skipped, stale)
                                    → 'neutral'
    """
    conclusion = run.get('conclusion')
    status = run.get('status')
    if conclusion == 'success':
        return 'success'
    if conclusion in {'failure', 'cancelled', 'timed_out', 'action_required'}:
        return 'failure'
    if status in {'queued', 'in_progress', 'waiting'}:
        return 'pending'
    return 'neutral'


def _qualified_repo(repo: str) -> str:
    """``leartech-orchestrator`` → ``mikelear/leartech-orchestrator``.

    Mirrors the convention used by other MCP servers in this directory.
    """
    return repo if '/' in repo else f'mikelear/{repo}'


async def _fetch_snapshot(client: httpx.AsyncClient, pr_repo: str, pr_number: int, token: str) -> PRSnapshot:
    """Pull PR + check-runs from GitHub and assemble a snapshot."""
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    qualified = _qualified_repo(pr_repo)

    pr_resp = await client.get(
        f'{GITHUB_API}/repos/{qualified}/pulls/{pr_number}',
        headers=headers,
    )
    pr_resp.raise_for_status()
    pr = pr_resp.json()

    labels = frozenset(label['name'] for label in pr.get('labels', []))
    merged = bool(pr.get('merged'))
    head_sha = str(pr['head']['sha'])
    mergeable_raw = pr.get('mergeable')
    mergeable: bool | None = bool(mergeable_raw) if isinstance(mergeable_raw, bool) else None

    cr_resp = await client.get(
        f'{GITHUB_API}/repos/{qualified}/commits/{head_sha}/check-runs',
        headers=headers,
        params={'per_page': 100},
    )
    cr_resp.raise_for_status()
    check_runs = cr_resp.json().get('check_runs', [])

    checks: dict[str, CheckState] = {}
    for run in check_runs:
        name = run.get('name', '')
        if not name:
            continue
        # If the same check name appears twice (re-run), keep the freshest result.
        # The GitHub API returns latest first by default, so the first wins.
        if name in checks:
            continue
        checks[name] = _check_state_for(run)

    return PRSnapshot(
        labels=labels,
        checks=checks,
        merged=merged,
        head_sha=head_sha,
        mergeable=mergeable,
    )


def _snapshot_payload(snap: PRSnapshot) -> dict[str, Any]:
    """Serialise a snapshot + derived rule outputs into the MCP response shape."""
    return {
        'stage': classify_stage(snap).value,
        'blocking_predicate': blocking_predicate(snap),
        'labels': sorted(snap.labels),
        'checks': dict(snap.checks),
        'merged': snap.merged,
        'head_sha': snap.head_sha,
        'mergeable': snap.mergeable,
    }


def _require_token() -> str:
    """Resolve GH_TOKEN once, raising a clear error if it's missing.

    The MCP server is invoked by both dev-agent and orchestrator. Both
    have the token wired by the chart; missing-env is an operational bug
    and should surface as a loud error, not a silent 401.
    """
    token = os.environ.get('GH_TOKEN')
    if not token:
        raise RuntimeError(
            'GH_TOKEN is not set in the agent environment. '
            'leartech-jx3-flow needs a token with pull_requests:read + checks:read '
            'on the target repo. Check the chart externalSecrets wiring.'
        )
    return token


@tool(
    'get_pr_jx3_stage',
    'Return the current JX3 stage + blocking predicate for a PR. '
    'Stages: pr_open_building, pr_checks_failing, pr_awaiting_approval, pr_held, '
    'pr_ready_to_merge, pr_merged_releasing, release_building, gitops_pr_open, '
    'rolled_to_cluster, unknown. Returns: {stage, blocking_predicate (str|null), '
    'labels (sorted list), checks (name→state map), merged, head_sha, mergeable}. '
    'Single GitHub round-trip — safe to call frequently.',
    {'pr_repo': str, 'pr_number': int},
)
async def _get_pr_jx3_stage(args: dict[str, Any]) -> dict[str, Any]:
    pr_repo = str(args['pr_repo'])
    pr_number = int(args['pr_number'])
    token = _require_token()
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
        snap = await _fetch_snapshot(client, pr_repo, pr_number, token)
    return {'content': [{'type': 'text', 'text': json.dumps(_snapshot_payload(snap), indent=2)}]}


@tool(
    'list_required_actions',
    'Return the chatops commands that would unblock this PR. '
    'Each entry: {command, scope, why}. Empty list = PR is merging/merged or '
    'in the "Tide will land it" state (no operator action needed). '
    'Typical outputs: [{command: "/retest", ...}] when multiple checks fail, '
    '[{command: "/hold cancel", ...}] when the merge-hold label is set.',
    {'pr_repo': str, 'pr_number': int},
)
async def _list_required_actions(args: dict[str, Any]) -> dict[str, Any]:
    pr_repo = str(args['pr_repo'])
    pr_number = int(args['pr_number'])
    token = _require_token()
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
        snap = await _fetch_snapshot(client, pr_repo, pr_number, token)
    actions = required_actions(snap)
    return {'content': [{'type': 'text', 'text': json.dumps(actions, indent=2)}]}


@tool(
    'wait_for_merge',
    'Poll the PR every 30s until it is merged, a terminal failure happens, or '
    'the timeout fires. Use this when the agent needs to BLOCK on a downstream '
    'PR — e.g. Orchestrator waiting for a dev-agent PR to merge before firing '
    'the next initiative. Returns: {final_stage, merged, blocking_predicate, '
    'snapshot}. Default timeout 1800s; never set above 3600s (the MCP server '
    'is in-process and very long blocks hurt session liveness).',
    {'pr_repo': str, 'pr_number': int, 'timeout_s': int},
)
async def _wait_for_merge(args: dict[str, Any]) -> dict[str, Any]:
    pr_repo = str(args['pr_repo'])
    pr_number = int(args['pr_number'])
    timeout_s = int(args.get('timeout_s') or _DEFAULT_WAIT_TIMEOUT_S)
    token = _require_token()
    deadline = time.monotonic() + max(1, timeout_s)
    final_snap: PRSnapshot | None = None
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
        while time.monotonic() < deadline:
            snap = await _fetch_snapshot(client, pr_repo, pr_number, token)
            final_snap = snap
            stage = classify_stage(snap)
            # Terminal stages: PR landed, or a required check is hard-failed.
            if stage in (JX3Stage.PR_MERGED_RELEASING, JX3Stage.PR_CHECKS_FAILING):
                payload = {
                    'final_stage': stage.value,
                    'merged': snap.merged,
                    'blocking_predicate': blocking_predicate(snap),
                    'snapshot': _snapshot_payload(snap),
                }
                return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}
            await asyncio.sleep(_POLL_INTERVAL_S)
    # Timed out — return the last snapshot we observed.
    snap_for_tail = final_snap
    if snap_for_tail is None:
        # Edge case: we never managed a successful fetch within the timeout
        # window. Make one last try so the caller has *something* concrete.
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            snap_for_tail = await _fetch_snapshot(client, pr_repo, pr_number, token)
    payload = {
        'final_stage': 'timeout',
        'merged': snap_for_tail.merged,
        'blocking_predicate': (f'Timed out after {timeout_s}s. Last stage: {classify_stage(snap_for_tail).value}'),
        'snapshot': _snapshot_payload(snap_for_tail),
    }
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


def build_jx3_flow_server() -> McpSdkServerConfig:
    """Build the jx3_flow SDK MCP server.

    Three tools wired: get_pr_jx3_stage, list_required_actions, wait_for_merge.
    """
    return create_sdk_mcp_server(
        name='leartech-jx3-flow',
        version='0.1.0',
        tools=[_get_pr_jx3_stage, _list_required_actions, _wait_for_merge],
    )
