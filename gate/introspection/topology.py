"""Mermaid generators for the platform topology diagrams.

Two diagrams are produced today:

- ``render_topology('full')`` — the four-phase shape from
  ``project_full_cycle_and_code_agent_contract.md`` (Phase 1 BA → Phase 2
  Architecture → Phase 3 Build → Phase 4 Feedback).
- ``render_topology('feedback')`` — zoomed view of the three feedback
  rings, all converging on the lessons catalog.

The catalog (mcp_servers + roles) is read lazily so the diagram reflects
the current YAML, not a snapshot baked into source.

Why these live in ``gate/`` rather than ``app/routers/``: pure Python,
no FastAPI dependency, unit-testable via a golden-file test
(``tests/test_topology_render.py``). The router is a thin shim.
"""

from __future__ import annotations

from typing import Literal

from gate.agent.mcp_catalog import load_catalog

TopologyScope = Literal['full', 'feedback']


def render_topology(scope: TopologyScope = 'full') -> str:
    """Return Mermaid source for one of the supported diagrams."""
    if scope == 'full':
        return _render_full()
    if scope == 'feedback':
        return _render_feedback()
    raise ValueError(f'Unknown topology scope {scope!r}; expected "full" or "feedback".')


def _render_full() -> str:
    catalog = load_catalog()
    role_names = sorted(catalog.roles)
    role_to_mcps = {name: catalog.roles[name].mcps for name in role_names}

    lines = ['graph TB']
    lines.append('  subgraph "Phase 1 — BA"')
    lines.append('    Lovable[Lovable mockup]')
    lines.append('    Stitch[Stitch design system]')
    lines.append('    Docs[Docs / customer ask]')
    if 'ba_agent' in role_to_mcps:
        lines.append('    BA[BA agent]')
        for mcp in role_to_mcps['ba_agent']:
            if 'lovable' in mcp:
                lines.append('    Lovable -.->|MCP| BA')
            elif 'stitch' in mcp:
                lines.append('    Stitch -.->|MCP| BA')
        lines.append('    Docs -.->|reads| BA')
    lines.append('  end')

    lines.append('  subgraph "Phase 2 — Architecture"')
    lines.append('    InitSet[initiative-set YAML]')
    lines.append('    SignOff{{Two-track sign-off}}')
    lines.append('    BA -->|outputs| InitSet')
    lines.append('    InitSet --> SignOff')
    lines.append('  end')

    lines.append('  subgraph "Phase 3 — Build"')
    lines.append('    Orch[DAG Orchestrator]')
    lines.append('    Code[Code Agent]')
    lines.append('    SignOff -->|approved| Orch')
    lines.append('    Orch -->|POST /initiatives| Code')
    lines.append('    Code -->|PR| Repo[(consumer repo)]')
    lines.append('  end')

    lines.append('  subgraph "Phase 4 — Feedback"')
    lines.append('    Gate[PR-gate ring]')
    lines.append('    Staging[Staging ring qa-arch]')
    lines.append('    Forensic[Forensic ring qa-arch]')
    lines.append('    Lessons[(lessons catalog)]')
    lines.append('    Repo --> Gate --> Lessons')
    lines.append('    Repo --> Staging --> Lessons')
    lines.append('    Staging --> Forensic --> Lessons')
    lines.append('    Lessons -.->|injected at session start| Code')
    lines.append('    Lessons -.-> BA')
    lines.append('  end')
    return '\n'.join(lines)


def _render_feedback() -> str:
    return '\n'.join(
        [
            'graph LR',
            '  PR[PR push] --> Ring1{Ring 1<br/>PR-gate}',
            '  Merge[merge to main] --> Stage[staging deploy]',
            '  Stage --> Ring2{Ring 2<br/>Staging}',
            '  Stage --> Ring3{Ring 3<br/>Forensic}',
            '  Ring1 -->|agent_run, ci_failure| Lessons[(lessons catalog)]',
            '  Ring2 -->|staging_test| Lessons',
            '  Ring3 -->|prod_incident| Lessons',
            '  Manual[manual /lesson comment] -->|manual_review| Lessons',
            '  Lessons -.->|calibration injected| Agent[Next agent session]',
        ]
    )


TOPOLOGY_DESCRIPTIONS: dict[TopologyScope, str] = {
    'full': ('Full leartech platform — Phase 1 (BA) through Phase 4 (feedback rings) with agent roles + MCP wiring.'),
    'feedback': (
        'The three concentric feedback rings — all converge on the lessons '
        'catalog, which calibrates the next agent session.'
    ),
}
