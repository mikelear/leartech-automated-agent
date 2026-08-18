"""Golden-file tests for the Mermaid topology generators.

The diagrams are read by humans (in CLI + dashboard renderings of
``leartech-agent topology``) and by mermaid.live's parser — both rely on
the exact shape staying stable across refactors. The golden files
capture the current rendering verbatim; deliberate diagram changes
require updating both the golden file and the topology generator.

To regenerate goldens after a deliberate change:

    uv run python -c "from gate.introspection.topology import render_topology; \
        print(render_topology('full'))" > tests/golden/topology_full.mmd
    uv run python -c "from gate.introspection.topology import render_topology; \
        print(render_topology('feedback'))" > tests/golden/topology_feedback.mmd
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.introspection.topology import TOPOLOGY_DESCRIPTIONS, render_topology

GOLDEN_DIR = Path(__file__).parent / 'golden'


def _golden(name: str) -> Path:
    return GOLDEN_DIR / f'topology_{name}.mmd'


@pytest.mark.parametrize('scope', ['full', 'feedback'])
def test_render_topology_matches_golden(scope: str) -> None:
    rendered = render_topology(scope)  # type: ignore[arg-type]
    expected_path = _golden(scope)
    expected = expected_path.read_text().rstrip('\n')
    assert rendered.rstrip('\n') == expected, (
        f'Topology drift for scope={scope!r}.\n'
        f'If this change is intentional, regenerate the golden:\n'
        f'  uv run python -c "from gate.introspection.topology import render_topology; '
        f"print(render_topology('{scope}'))\" > {expected_path}\n\n"
        f'--- expected (first 200 chars) ---\n{expected[:200]}\n'
        f'--- got (first 200 chars) ---\n{rendered[:200]}\n'
    )


def test_render_topology_full_includes_all_four_phases() -> None:
    rendered = render_topology('full')
    for label in ['Phase 1 — BA', 'Phase 2 — Architecture', 'Phase 3 — Build', 'Phase 4 — Feedback']:
        assert label in rendered


def test_render_topology_feedback_lists_three_rings() -> None:
    rendered = render_topology('feedback')
    assert 'Ring 1' in rendered
    assert 'Ring 2' in rendered
    assert 'Ring 3' in rendered
    assert 'lessons catalog' in rendered.lower()


def test_render_topology_unknown_scope_raises() -> None:
    with pytest.raises(ValueError, match='Unknown topology scope'):
        render_topology('not-a-scope')  # type: ignore[arg-type]


def test_descriptions_cover_every_scope() -> None:
    assert set(TOPOLOGY_DESCRIPTIONS) == {'full', 'feedback'}
