"""PR-back helper — opens a PR with a YAML change to a target repo.

Pure async utility; no FastAPI imports. Used by the MCP admin endpoints to make
all catalog mutations durable via GitOps (PR → merge → redeploy). Every change
goes through a human-reviewed PR; in-memory state is never mutated.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

_PR_NUMBER_RE = re.compile(r'/pull/(\d+)$')


async def _run(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess asynchronously, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode is None:
        raise RuntimeError('subprocess returncode is None after communicate()')
    return proc.returncode, stdout_bytes.decode(), stderr_bytes.decode()


async def open_yaml_change_pr(
    repo: str,
    base_branch: str,
    new_branch: str,
    file_path: str,
    new_yaml_content: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
) -> dict[str, str | int]:
    """Open a PR with the given YAML change. Returns {pr_url, pr_number, branch}.

    Uses a /tmp/ workdir — never /workspace (which is the agent's own clone).
    All git/gh operations are async (no blocking subprocess.run).
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'

    with tempfile.TemporaryDirectory(prefix='pr_back_') as tmpdir:
        clone_dir = str(Path(tmpdir) / 'repo')

        # Clone the target repo into a fresh subdirectory
        rc, _, stderr = await _run(
            'gh', 'repo', 'clone', qualified, clone_dir,
            '--', '--depth=1', f'--branch={base_branch}',
        )
        if rc != 0:
            raise RuntimeError(f'gh repo clone failed: {stderr.strip()}')

        # Create the PR branch
        rc, _, stderr = await _run('git', 'checkout', '-b', new_branch, cwd=clone_dir)
        if rc != 0:
            raise RuntimeError(f'git checkout -b failed: {stderr.strip()}')

        # Write the new file contents (mkdir -p for safety)
        target = Path(clone_dir) / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_yaml_content)

        # Stage the changed file
        rc, _, stderr = await _run('git', 'add', file_path, cwd=clone_dir)
        if rc != 0:
            raise RuntimeError(f'git add failed: {stderr.strip()}')

        # Commit
        rc, _, stderr = await _run('git', 'commit', '-m', commit_message, cwd=clone_dir)
        if rc != 0:
            raise RuntimeError(f'git commit failed: {stderr.strip()}')

        # Push branch to origin
        rc, _, stderr = await _run('git', 'push', '-u', 'origin', new_branch, cwd=clone_dir)
        if rc != 0:
            raise RuntimeError(f'git push failed: {stderr.strip()}')

        # Open the PR
        rc, stdout, stderr = await _run(
            'gh', 'pr', 'create',
            '--repo', qualified,
            '--base', base_branch,
            '--head', new_branch,
            '--title', pr_title,
            '--body', pr_body,
            cwd=clone_dir,
        )
        if rc != 0:
            raise RuntimeError(f'gh pr create failed: {stderr.strip()}')

        pr_url = stdout.strip()
        match = _PR_NUMBER_RE.search(pr_url)
        if not match:
            raise RuntimeError(f'Could not parse PR number from gh pr create output: {pr_url!r}')
        pr_number = int(match.group(1))

        return {
            'pr_url': pr_url,
            'pr_number': pr_number,
            'branch': new_branch,
        }
