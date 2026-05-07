"""Playwright coverage-gap criterion + AI-drafted spec suggestion (the wow piece).

When a PR adds new UI surface (component / route / data-testid) that no existing
end2end-ui spec exercises, fail the criterion AND attach a Claude-drafted proposed
spec so the human reviewer can copy-paste-adapt-commit.

Skips cleanly when:
- No UI surface was added (spec-only / config-only PRs)
- ANTHROPIC_API_KEY isn't set (suggester needs it; criterion still fails on gap, just without a suggestion)
- end2end-ui/ directory doesn't exist yet (repo not opted in)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.tools import (
    PRContext,
    compute_ui_surface_delta,
    fetch_pr_diff,
    inventory_specs,
    is_anthropic_key_present,
    suggest_spec,
)

pytestmark = pytest.mark.playwright

# Number of existing specs to feed Claude as structural reference. Keeps prompt size bounded.
REFERENCE_SPEC_LIMIT = 2


def _consumer_repo_root(repo: str) -> Path:
    """Convention: ~/leartech/<repo-name>. Matches initiative.py's _default_repo_root."""
    return Path('~/leartech').expanduser() / repo.split('/')[-1]


def _read_reference_specs(specs_dir: Path) -> list[tuple[str, str]]:
    """Read up to REFERENCE_SPEC_LIMIT specs from the directory, ordered by name."""
    if not specs_dir.exists():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(specs_dir.glob('*.spec.ts'))[:REFERENCE_SPEC_LIMIT]:
        out.append((path.name, path.read_text(errors='replace')))
    return out


def _read_component_source(repo_root: Path, component_files: tuple[str, ...]) -> str | None:
    """Concatenate the source of newly-added component files (capped). Helps Claude understand intent."""
    if not component_files:
        return None
    parts: list[str] = []
    for rel_path in component_files[:2]:  # cap at 2 components in the prompt
        full = repo_root / rel_path
        if full.exists():
            parts.append(f'// {rel_path}\n{full.read_text(errors="replace")}')
    return '\n\n'.join(parts) if parts else None


def test_ui_changes_have_playwright_coverage(pr_context: PRContext) -> None:
    """If the PR added new UI surface, at least one end2end-ui spec must exercise it.

    On gap, the assertion message includes a Claude-drafted suggested spec when
    ANTHROPIC_API_KEY is set. Otherwise just lists the uncovered surface.
    """
    repo_root = _consumer_repo_root(pr_context.repo)
    diff = fetch_pr_diff(pr_context.repo, pr_context.number)
    delta = compute_ui_surface_delta(diff, repo_root=repo_root if repo_root.exists() else None)

    if delta.is_empty:
        pytest.skip('No UI surface added by this PR — spec-only or config-only change.')

    specs_dir = repo_root / 'end2end-ui'
    if not specs_dir.exists():
        pytest.skip(f'No end2end-ui/ directory at {specs_dir} — repo not opted in.')

    coverage = inventory_specs(specs_dir)

    # Find which new surface elements have NO existing coverage.
    uncovered_selectors = [s for s in delta.new_component_selectors if not coverage.covers_selector(s)]
    uncovered_testids = [t for t in delta.new_data_testids if not coverage.covers_testid(t)]
    uncovered_routes = [r for r in delta.new_route_paths if not coverage.covers_route(r)]

    if not (uncovered_selectors or uncovered_testids or uncovered_routes):
        return  # Every new surface element is referenced by at least one existing spec.

    # Build a clear failure message — and attempt a draft spec if we can.
    lines = ['Playwright coverage gap — new UI surface not exercised by any end2end-ui spec:']
    if uncovered_selectors:
        lines.append(f'  Components ({len(uncovered_selectors)}): ' + ', '.join(uncovered_selectors))
    if uncovered_testids:
        lines.append(f'  data-testid ({len(uncovered_testids)}): ' + ', '.join(uncovered_testids))
    if uncovered_routes:
        lines.append(f'  Routes ({len(uncovered_routes)}): ' + ', '.join(uncovered_routes))

    if is_anthropic_key_present():
        try:
            reference_specs = _read_reference_specs(specs_dir)
            component_source = _read_component_source(repo_root, delta.new_component_files)
            suggestion = suggest_spec(
                delta,
                reference_specs=reference_specs,
                component_source=component_source,
            )
            lines.append('')
            lines.append(f'AI-drafted proposed spec — `end2end-ui/{suggestion.filename}`:')
            lines.append(f'  Rationale: {suggestion.rationale}')
            lines.append('')
            lines.append('--- BEGIN SPEC ---')
            lines.append(suggestion.spec_body)
            lines.append('--- END SPEC ---')
        except Exception as exc:  # noqa: BLE001 — suggester is best-effort; don't mask the gap
            lines.append('')
            lines.append(f'(spec suggester unavailable: {exc.__class__.__name__}: {exc})')
    else:
        lines.append('')
        lines.append(
            '(Set ANTHROPIC_API_KEY via `leartech-claude-key` to get an AI-drafted proposed spec '
            'in this failure message.)'
        )

    has_gap = bool(uncovered_selectors or uncovered_testids or uncovered_routes)
    assert not has_gap, '\n'.join(lines)
