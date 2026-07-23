"""PR metadata via gh CLI. The criteria layer reads this through the `pr_context` pytest fixture."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class PRContext:
    repo: str
    number: int
    head_sha: str
    base_sha: str
    title: str
    body: str
    changed_files: tuple[str, ...]
    state: str

    @property
    def qualified_repo(self) -> str:
        return self.repo if '/' in self.repo else f'mikelear/{self.repo}'


def _gh(args: list[str]) -> str:
    result = subprocess.run(
        ['gh', *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def load_pr_context(repo: str, pr_number: int) -> PRContext:
    qualified = repo if '/' in repo else f'mikelear/{repo}'

    # gh pr view exposes head/title/body/state/files but not the base SHA. Use the REST
    # API for the canonical PR object (head.sha + base.sha + state) and gh pr view for files.
    pr_raw = _gh(['api', f'repos/{qualified}/pulls/{pr_number}'])
    pr = json.loads(pr_raw)

    files_raw = _gh(['pr', 'view', str(pr_number), '-R', qualified, '--json', 'files'])
    files = json.loads(files_raw).get('files', [])

    return PRContext(
        repo=qualified,
        number=int(pr['number']),
        head_sha=pr['head']['sha'],
        base_sha=pr['base']['sha'],
        title=pr['title'],
        body=pr.get('body') or '',
        changed_files=tuple(f['path'] for f in files),
        # GitHub REST returns lowercase ('open'/'closed'); normalise to gh pr view's uppercase.
        state=pr['state'].upper(),
    )


def _commit_message(commit: object) -> str:
    """Extract the headline + body from a single `gh pr view --json commits` entry.

    Both fields may be absent (empty commits, imported histories). Skip cleanly.
    """
    if not isinstance(commit, dict):
        return ''
    headline = commit.get('messageHeadline') or ''
    body = commit.get('messageBody') or ''
    parts = [str(headline).strip(), str(body).strip()]
    return '\n'.join(p for p in parts if p)


def fetch_pr_commit_messages(repo: str, pr_number: int) -> str:
    """Return the concatenated headline+body of every commit in the PR as one string.

    Powers the ``chart-flip → overlay-PR`` criterion (and any future criterion
    that needs to scan commit messages for markers a PR author would drop in a
    commit but not necessarily in the PR title/body).

    Returns an empty string on any ``gh`` failure — a criterion depending on
    this signal can then fall back to title/body-only evidence without
    exploding the whole gate run.
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    try:
        raw = _gh(['pr', 'view', str(pr_number), '-R', qualified, '--json', 'commits'])
    except RuntimeError:
        return ''
    if not raw.strip():
        return ''
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ''
    commits_raw = payload.get('commits') if isinstance(payload, dict) else None
    if not isinstance(commits_raw, Iterable):
        return ''
    messages = [_commit_message(c) for c in commits_raw]
    return '\n\n'.join(m for m in messages if m)
