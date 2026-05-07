"""Inventory of what existing Playwright specs cover.

Walks `end2end-ui/*.spec.ts` and extracts every (component selector, data-testid,
route path) the specs reference. Used by `test_ui_changes_have_playwright_coverage`
to compare new UI surface against what's already covered.

Conservative parser — uses regex, not full TS AST. Misses dynamic selectors, accepts
some false positives. That's fine: the criterion's job is to flag *probable* gaps,
not certify a clean inventory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# `page.goto('/login')` / `await page.goto("/")`
_GOTO_RE = re.compile(r"page\.goto\s*\(\s*['\"`]([^'\"`]+)['\"`]")

# `getByTestId('foo')` / `getByTestId("foo")`
_GET_BY_TESTID_RE = re.compile(r"getByTestId\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

# `[data-testid="foo"]` inside locator() or equivalent — covers wrapper-attribute style
_LOCATOR_TESTID_RE = re.compile(r"\bdata-testid\s*=\s*['\"]([^'\"]+)['\"]")

# `locator('app-login')` / `locator('app-foo .bar')` — catches Angular component selectors
_LOCATOR_COMPONENT_RE = re.compile(r"locator\s*\(\s*['\"](app-[a-z][a-z0-9-]*)")


@dataclass(frozen=True)
class SpecCoverage:
    """What an existing Playwright suite already exercises."""

    routes: frozenset[str] = field(default_factory=frozenset)
    data_testids: frozenset[str] = field(default_factory=frozenset)
    component_selectors: frozenset[str] = field(default_factory=frozenset)
    spec_count: int = 0

    def covers_route(self, path: str) -> bool:
        # Match exact OR prefix (route registered as 'two-factor' covers '/two-factor/setup' etc.)
        normalized = path.lstrip('/')
        for covered in self.routes:
            if covered.lstrip('/') == normalized or normalized.startswith(covered.lstrip('/') + '/'):
                return True
        return False

    def covers_testid(self, testid: str) -> bool:
        return testid in self.data_testids

    def covers_selector(self, selector: str) -> bool:
        return selector in self.component_selectors


def parse_spec(text: str) -> tuple[set[str], set[str], set[str]]:
    """Returns (routes, data_testids, component_selectors) referenced in a single spec body."""
    routes = {match.group(1) for match in _GOTO_RE.finditer(text)}
    testids = {match.group(1) for match in _GET_BY_TESTID_RE.finditer(text)} | {
        match.group(1) for match in _LOCATOR_TESTID_RE.finditer(text)
    }
    selectors = {match.group(1) for match in _LOCATOR_COMPONENT_RE.finditer(text)}
    return routes, testids, selectors


def inventory_specs(specs_dir: Path) -> SpecCoverage:
    """Walk every `*.spec.ts` in the directory and aggregate the coverage signals."""
    if not specs_dir.exists():
        return SpecCoverage()
    routes: set[str] = set()
    testids: set[str] = set()
    selectors: set[str] = set()
    spec_count = 0
    for path in sorted(specs_dir.rglob('*.spec.ts')):
        spec_count += 1
        r, t, s = parse_spec(path.read_text(errors='replace'))
        routes |= r
        testids |= t
        selectors |= s
    return SpecCoverage(
        routes=frozenset(routes),
        data_testids=frozenset(testids),
        component_selectors=frozenset(selectors),
        spec_count=spec_count,
    )
