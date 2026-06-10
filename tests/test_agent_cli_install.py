"""Pinning tests for the `leartech-agent` console_script installation.

Background: PR #95 added the `leartech-agent` operator CLI as a
`[project.scripts]` entry in pyproject.toml. The deployed image then
shipped without the binary on PATH — `which leartech-agent` returned
1 inside the pod. Two root causes were possible:

  1. The pyproject entry's module path drifted from the actual callable
     (e.g. a refactor renaming the click `cli` symbol).
  2. The Dockerfile's `uv sync` step didn't materialise the script onto
     the venv's PATH (or the agent user's shell PATH didn't include
     `/app/.venv/bin`).

These tests pin both invariants at the source-tree level so the next
regression fails fast in CI, not at `kubectl exec` time.

The Dockerfile half (PATH extension) is verified by `scripts/e2e.sh`
where a real venv is available; here we only cover what's
introspectable without a build step.
"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
PYPROJECT = REPO_ROOT / 'pyproject.toml'
DOCKERFILE = REPO_ROOT / 'Dockerfile'

EXPECTED_ENTRY_POINT = 'app.agent_cli.main:cli'


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))


def test_pyproject_declares_leartech_agent_console_script() -> None:
    """`[project.scripts]` must carry the operator CLI entry — without it
    `uv sync` produces no `leartech-agent` binary in `.venv/bin/`."""
    cfg = _load_pyproject()
    scripts = cfg.get('project', {}).get('scripts', {})
    assert 'leartech-agent' in scripts, (
        '`[project.scripts] leartech-agent` is missing from pyproject.toml — '
        'the operator CLI will not be installed into the venv by `uv sync`. '
        'Restore the entry: leartech-agent = "app.agent_cli.main:cli"'
    )
    assert scripts['leartech-agent'] == EXPECTED_ENTRY_POINT, (
        f'`leartech-agent` entry points at {scripts["leartech-agent"]!r}; '
        f'expected {EXPECTED_ENTRY_POINT!r}. If the CLI module moved, update '
        'both this test and the e2e smoke that invokes the bare command.'
    )


def test_entry_point_target_is_importable_and_callable() -> None:
    """The `module:attr` target in pyproject must resolve at import time —
    if it doesn't, `uv sync` will still write the wrapper script, but
    invoking `leartech-agent` raises ModuleNotFoundError / AttributeError
    on first run. Catch that here instead."""
    module_path, _, attr = EXPECTED_ENTRY_POINT.partition(':')
    # Re-import cleanly even if pytest's earlier collection cached a parent
    # package — keeps the test deterministic across reruns in-process.
    sys.modules.pop(module_path, None)
    module = importlib.import_module(module_path)
    target = getattr(module, attr, None)
    assert target is not None, (
        f'pyproject `[project.scripts]` points at {EXPECTED_ENTRY_POINT}, '
        f'but `{attr}` is not defined in {module_path}. The console_script '
        'wrapper will install but fail with AttributeError at runtime.'
    )
    assert callable(target), (
        f'{EXPECTED_ENTRY_POINT} resolved to {target!r} which is not callable. '
        'Click groups + commands are callable; a misconfigured re-export is not.'
    )


def test_dockerfile_extends_path_with_venv_bin() -> None:
    """The deployed image must put `/app/.venv/bin` on PATH so operators'
    `kubectl exec pod -- leartech-agent ...` finds the binary. Without
    this, the CLI is materialised by `uv sync` but unreachable from a
    login shell — which is exactly the bug PR #95 shipped with."""
    contents = DOCKERFILE.read_text(encoding='utf-8')
    # Match either the bare quote-less form or the safer expanded form
    # (with or without the `${PATH}` continuation).
    assert '/app/.venv/bin' in contents, (
        'Dockerfile no longer references `/app/.venv/bin` — without putting '
        'the venv scripts dir on PATH, console_scripts produced by `uv sync` '
        '(notably `leartech-agent`) are not callable inside the pod.'
    )
    # Belt-and-braces: ensure it's in an `ENV PATH=` directive, not just a
    # comment or a runtime `RUN`. The full-line match keeps this stable
    # under reformatting that splits the value across lines.
    path_env_lines = [line for line in contents.splitlines() if line.strip().startswith('ENV PATH=')]
    assert any('/app/.venv/bin' in line for line in path_env_lines), (
        'Found `/app/.venv/bin` in Dockerfile but not inside an `ENV PATH=` '
        'directive — only ENV-set values persist into the running container.'
    )
