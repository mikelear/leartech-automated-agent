"""Catalog coverage — every real file in the repo's catalogs loads cleanly.

The existing unit tests (`test_lessons.py`, `test_initiative_loader.py`,
`test_mcp_catalog.py`) verify loader behaviour against synthetic test data. These
tests verify the *production* catalogs — every lesson .md, every initiative .yaml,
every MCP entry — round-trips through the production loader without error.

Catches the silent-drift case where someone adds a malformed real file and the
synthetic-data unit tests stay green because they never touch the real catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.agent.lessons.loader import CATALOG_DIR as LESSONS_DIR
from gate.agent.lessons.loader import parse_lesson_file
from gate.agent.mcp_catalog import load_catalog
from gate.initiatives.loader import load_initiative

REPO_ROOT = Path(__file__).parent.parent
INITIATIVES_DIR = REPO_ROOT / 'initiatives'


# ─── Lessons ─────────────────────────────────────────────────────────────


def _lesson_files() -> list[Path]:
    return sorted(LESSONS_DIR.glob('*.md'))


def test_at_least_one_lesson_exists() -> None:
    """Smoke check that the catalog isn't empty — guards against an accidental wipe."""
    files = _lesson_files()
    assert len(files) >= 20, f'Lessons catalog suspiciously small: {len(files)} files'


@pytest.mark.parametrize('lesson_path', _lesson_files(), ids=lambda p: p.stem)
def test_every_lesson_parses_cleanly(lesson_path: Path) -> None:
    """Production loader must parse every catalog file without error."""
    lesson = parse_lesson_file(lesson_path)

    assert lesson.id, f'{lesson_path.name}: empty id field'
    assert lesson.title, f'{lesson_path.name}: empty title field'
    assert lesson.body.strip(), f'{lesson_path.name}: empty body'


@pytest.mark.parametrize('lesson_path', _lesson_files(), ids=lambda p: p.stem)
def test_lesson_id_matches_filename(lesson_path: Path) -> None:
    """File `foo-bar.md` must contain `id: foo-bar` — keeps catalog scannable."""
    lesson = parse_lesson_file(lesson_path)
    assert lesson.id == lesson_path.stem, f'{lesson_path.name}: id={lesson.id!r} does not match filename stem'


@pytest.mark.parametrize('lesson_path', _lesson_files(), ids=lambda p: p.stem)
def test_calibration_lessons_have_applies_to(lesson_path: Path) -> None:
    """Calibration lessons must declare which roles they reach.

    `category: architecture` lessons are historical/design records and may have
    empty `applies_to` — they're not meant to be injected into agent prompts.
    Other categories without `applies_to` reach no agent and are almost certainly
    a mistake.
    """
    lesson = parse_lesson_file(lesson_path)
    if lesson.category == 'architecture':
        return
    assert lesson.applies_to, (
        f'{lesson_path.name}: category={lesson.category!r} with empty applies_to — this lesson reaches no agent role'
    )


# ─── Initiatives ─────────────────────────────────────────────────────────


def _initiative_files() -> list[Path]:
    return sorted(INITIATIVES_DIR.glob('*.yaml'))


def test_at_least_one_initiative_exists() -> None:
    files = _initiative_files()
    assert len(files) >= 5, f'Initiatives catalog suspiciously small: {len(files)} files'


@pytest.mark.parametrize('initiative_path', _initiative_files(), ids=lambda p: p.stem)
def test_every_initiative_loads_cleanly(initiative_path: Path) -> None:
    """Production loader must parse every initiative without error."""
    initiative = load_initiative(initiative_path)

    assert initiative.name, f'{initiative_path.name}: empty name field'
    assert initiative.goal, f'{initiative_path.name}: empty goal field'
    assert initiative.repos, f'{initiative_path.name}: no repos resolved'


@pytest.mark.parametrize('initiative_path', _initiative_files(), ids=lambda p: p.stem)
def test_initiative_name_matches_filename(initiative_path: Path) -> None:
    """File `foo-bar.yaml` must contain `name: foo-bar` — keeps lookup unambiguous."""
    initiative = load_initiative(initiative_path)
    assert initiative.name == initiative_path.stem, (
        f'{initiative_path.name}: name={initiative.name!r} does not match filename stem'
    )


# ─── MCP catalog ─────────────────────────────────────────────────────────


def test_mcp_catalog_loads() -> None:
    """Production catalog must load without error."""
    catalog = load_catalog()
    assert catalog.mcp_servers, 'No MCP servers in catalog'
    assert catalog.roles, 'No roles in catalog'


def test_every_role_mcp_reference_exists() -> None:
    """Every role's `mcps:` entry must reference a real MCP server."""
    catalog = load_catalog()
    server_names = set(catalog.mcp_servers)

    for role_name, role in catalog.roles.items():
        for mcp_name in role.mcps:
            assert mcp_name in server_names, (
                f'role {role_name!r} references unknown MCP {mcp_name!r}. Known: {sorted(server_names)}'
            )


def test_every_sdk_mcp_builder_imports() -> None:
    """sdk-type MCP builders must be importable — catches typos in the dotted path.

    Doesn't *invoke* the builder (that's `test_every_sdk_mcp_builds_via_catalog`).
    Just resolves the `module:function` reference and asserts the function exists.
    """
    import importlib

    catalog = load_catalog()
    for name, mcp in catalog.mcp_servers.items():
        if mcp.type != 'sdk':
            continue
        assert mcp.builder, f'{name}: sdk MCP missing builder field'
        module_path, _, function_name = mcp.builder.partition(':')
        assert module_path and function_name, f'{name}: builder {mcp.builder!r} not in expected `module:function` shape'
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            pytest.fail(f'{name}: cannot import {module_path}: {exc}')
        assert hasattr(module, function_name), f'{name}: {module_path} has no `{function_name}`'


def test_every_sdk_mcp_builds_via_catalog() -> None:
    """End-to-end catalog → live MCP round-trip.

    For every sdk-type MCP in the production catalog: resolve the builder via
    the catalog's `builder` field, invoke it, assert it returns a non-None
    McpSdkServerConfig. Catches the case where the builder imports cleanly
    but raises at runtime (e.g. missing imports inside the function, schema
    errors, etc.).
    """
    import importlib

    catalog = load_catalog()
    for name, mcp in catalog.mcp_servers.items():
        if mcp.type != 'sdk':
            continue
        assert mcp.builder is not None, f'{name}: sdk MCP missing builder field'
        module_path, _, function_name = mcp.builder.partition(':')
        module = importlib.import_module(module_path)
        builder = getattr(module, function_name)
        try:
            server = builder()
        except Exception as exc:
            pytest.fail(f'{name}: builder {mcp.builder} raised: {exc!r}')
        assert server is not None, f'{name}: builder returned None'
