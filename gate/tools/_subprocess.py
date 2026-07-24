"""Shared subprocess + config helpers for the repo-factory / cluster-wiring tools.

Extracted so ``repo_factory`` and ``source_config`` share one ``run`` implementation
instead of duplicating it (the two were byte-identical). ``_default_org`` keeps the GitHub
org out of literals — env-driven per the leartech no-hardcoded-value convention.
"""

from __future__ import annotations

import os
import subprocess


def run(args: list[str], cwd: str | os.PathLike[str] | None = None) -> str:
    """Run a command, return stdout; raise RuntimeError with stderr on non-zero exit."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'{" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def default_org() -> str:
    """GitHub org for created/registered repos — ``LEARTECH_GITHUB_ORG`` env, default mikelear."""
    return os.environ.get('LEARTECH_GITHUB_ORG', 'mikelear')
