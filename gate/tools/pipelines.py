"""Tekton pipeline status across both clusters — native gh REST parsing.

Originally wrapped `~/leartech/Hub/scripts/pr-pipelines.sh --json`, but that bound the
runner to a specific laptop layout (~/leartech/Hub) and made cluster-pod deployment
awkward. Now we go straight to gh's `statusCheckRollup` and derive cluster + pipelinerun
from the `targetUrl` ourselves.

`parse_pipelines_json` is kept for callers who still feed raw output from the bash script
(it tolerates the trailing failing-step detail lines that script emits). New code should
prefer `list_pr_checks` directly.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

TERMINAL_STATES = {'SUCCESS', 'FAILURE', 'ERROR'}
FAILED_STATES = {'FAILURE', 'ERROR'}

# tekton-dashboard-jx.<sub>.leartech.com — sub maps to cluster.
# Extend this when a new cluster comes online (mirror the bash script's `cluster_for_subdomain`).
_SUBDOMAIN_TO_CLUSTER = {
    'jx': 'gcp',
    'az': 'az',
}

# .../pipelineruns/<name> — appears in either path or fragment of the dashboard URL.
_PIPELINERUN_RE = re.compile(r'/pipelineruns/([^/?#]+)')


@dataclass(frozen=True)
class PipelineCheck:
    cluster: str  # 'gcp' | 'az'
    check: str  # e.g. 'pr', 'lint', 'ai-review', 'end2end-ui'
    state: str  # 'SUCCESS' | 'FAILURE' | 'ERROR' | 'PENDING' | 'IN_PROGRESS'
    pipelinerun: str  # Tekton pipelineRun resource name

    @property
    def passed(self) -> bool:
        return self.state == 'SUCCESS'

    @property
    def failed(self) -> bool:
        return self.state in FAILED_STATES

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def parse_target_url(target_url: str) -> tuple[str, str] | None:
    """Extract (cluster, pipelinerun_name) from a Tekton-dashboard URL. None if unrecognised.

    URL shape: ``https://tekton-dashboard-jx.<sub>.leartech.com/#/namespaces/jx/pipelineruns/<name>``
    """
    if not target_url:
        return None
    parsed = urlparse(target_url)
    host_parts = (parsed.hostname or '').split('.')
    if len(host_parts) < 4 or host_parts[0] != 'tekton-dashboard-jx':
        return None
    cluster = _SUBDOMAIN_TO_CLUSTER.get(host_parts[1])
    if cluster is None:
        return None
    # gh dashboard URLs put pipelinerun in the URL fragment (#/...) for SPAs;
    # legacy / non-SPA forms put it in the path. Try both.
    for source in (parsed.fragment, parsed.path):
        match = _PIPELINERUN_RE.search(source)
        if match:
            return cluster, match.group(1)
    return None


def parse_status_check_rollup(rollup: list[dict[str, Any]]) -> list[PipelineCheck]:
    """Convert gh's `.statusCheckRollup` array into typed PipelineCheck rows.

    Skips entries with no targetUrl (e.g. GitHub Actions checks if any), or unrecognised
    URL shapes (defensively — if cluster mapping changes, we want known-clusters-only).
    """
    checks: list[PipelineCheck] = []
    for entry in rollup:
        context = entry.get('context')
        state = entry.get('state')
        if not context or not state:
            continue
        url_parts = parse_target_url(entry.get('targetUrl', ''))
        if url_parts is None:
            continue
        cluster, pipelinerun = url_parts
        # Strip cluster prefix from check name: 'gcp/lint' -> 'lint'.
        check_name = context.removeprefix(f'{cluster}/')
        checks.append(
            PipelineCheck(cluster=cluster, check=check_name, state=state, pipelinerun=pipelinerun),
        )
    return checks


def parse_pipelines_json(stdout: str) -> list[dict[str, Any]]:
    """Parse pr-pipelines.sh `--json` stdout into a list of row dicts.

    Kept for callers that still feed raw bash-script output. Tolerates the trailing
    non-JSON text (`✗ gcp/end2end-ui failing-step=...`) the script emits after the JSON
    array. Empty input → empty list.

    New code should call `list_pr_checks` directly (no bash dependency).
    """
    text = (stdout or '').lstrip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(text)
    if not isinstance(data, list):
        raise ValueError(f'expected JSON array at start of pr-pipelines.sh output, got {type(data).__name__}')
    return data


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def list_pr_checks(repo: str, pr_number: int, cluster: str = 'both') -> list[PipelineCheck]:
    """Fetch + parse Tekton checks on a PR via `gh pr view --json statusCheckRollup`.

    `cluster` filter accepts 'gcp', 'az', or 'both' (default).
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    raw = _gh(
        ['pr', 'view', str(pr_number), '-R', qualified, '--json', 'statusCheckRollup', '-q', '.statusCheckRollup']
    )
    rollup = json.loads(raw or '[]')
    if not isinstance(rollup, list):
        return []
    checks = parse_status_check_rollup(rollup)
    if cluster != 'both':
        checks = [c for c in checks if c.cluster == cluster]
    return checks
