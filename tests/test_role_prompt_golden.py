"""Golden snapshot tests for per-role lesson rendering.

Locks the rendered calibration block each agent role sees. Future changes to
lesson content (added, removed, edited) WILL change the rendered output and
fail these tests — forcing an explicit decision to re-snapshot. This surfaces
calibration drift that would otherwise be silent.

## Updating the snapshots

When a lesson change is intentional:

    REFRESH_GOLDEN=1 uv run pytest tests/test_role_prompt_golden.py

This rewrites the snapshots under `tests/golden/`. Commit them in the same PR
as the lesson change so reviewers see exactly how the prompt shifted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gate.agent.lessons.prompt_renderer import render_for
from gate.agent.mcp_catalog import load_catalog

GOLDEN_DIR = Path(__file__).parent / 'golden'
REFRESH = os.environ.get('REFRESH_GOLDEN') == '1'


def _all_roles() -> list[str]:
    """Pull role names from the production catalog — keeps golden coverage in sync.

    If a new role gets added to mcp_catalog.yaml, this test starts producing a
    golden for it automatically on next refresh.
    """
    return sorted(load_catalog().roles)


@pytest.mark.parametrize('role', _all_roles())
def test_role_prompt_golden(role: str) -> None:
    """Rendered calibration block for each role must match its stored snapshot."""
    GOLDEN_DIR.mkdir(exist_ok=True)
    rendered = render_for(role)
    snapshot_path = GOLDEN_DIR / f'role_{role}.md'

    if REFRESH:
        snapshot_path.write_text(rendered + '\n' if rendered else '')
        pytest.skip(f'Refreshed snapshot for {role}')

    if not snapshot_path.exists():
        snapshot_path.write_text(rendered + '\n' if rendered else '')
        pytest.fail(
            f'Snapshot {snapshot_path.name} did not exist — created with current output. '
            f'Re-run to verify, then commit the snapshot.'
        )

    expected = snapshot_path.read_text().rstrip('\n')
    actual = rendered

    assert actual == expected, (
        f'Rendered prompt for role={role!r} differs from snapshot.\n'
        f'If this change is intentional, run: REFRESH_GOLDEN=1 uv run pytest '
        f'tests/test_role_prompt_golden.py\n'
        f'Then commit {snapshot_path} in the same PR as the lesson change.'
    )
