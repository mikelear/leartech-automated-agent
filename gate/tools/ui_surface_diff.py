"""Extract the *new UI surface* introduced by a PR's diff.

UI surface = things a Playwright spec might reasonably want to assert against:

- New Angular components (added `*.component.ts` files) — known by their `selector`
- New `data-testid` attributes (in templates / inline templates of new lines)
- New routes (added entries to a routing module — heuristic; not exhaustive)

Pure functions on the diff text. The criterion that consumes this also reads the
consumer repo to look up the component's `selector` (since the diff alone may not
include the decorator if the file was added in a previous commit on the branch).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# `@Component({ ... selector: 'app-foo', ... })` — single-quote / double-quote / backtick variants
_SELECTOR_RE = re.compile(r"selector\s*:\s*['\"`]([^'\"`]+)['\"`]")

# `data-testid="foo"` or `data-testid='foo'` — only on `+` lines (new content).
_DATA_TESTID_RE = re.compile(r"\bdata-testid\s*=\s*['\"]([^'\"]+)['\"]")

# Route entries: `{ path: 'foo', ... }`. Heuristic — only matches simple shapes.
_ROUTE_PATH_RE = re.compile(r"\bpath\s*:\s*['\"]([^'\"]+)['\"]")


@dataclass(frozen=True)
class UISurfaceDelta:
    """What a PR added to the UI surface."""

    new_component_files: tuple[str, ...] = field(default_factory=tuple)
    new_component_selectors: tuple[str, ...] = field(default_factory=tuple)
    new_data_testids: tuple[str, ...] = field(default_factory=tuple)
    new_route_paths: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not (
            self.new_component_files or self.new_component_selectors or self.new_data_testids or self.new_route_paths
        )


def added_files_in_diff(diff: str, suffix: str) -> list[str]:
    """Files newly created in the PR matching `suffix`. Detects `new file mode` markers."""
    files: list[str] = []
    in_new_file = False
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith('diff --git'):
            in_new_file = False
            current_path = None
        elif line.startswith('new file mode'):
            in_new_file = True
        elif line.startswith('+++ b/') and in_new_file:
            current_path = line[6:]
            if current_path.endswith(suffix):
                files.append(current_path)
            current_path = None
            in_new_file = False
    return files


def added_lines(diff: str) -> list[str]:
    """Content of `+` lines (new content), excluding diff headers."""
    return [line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++')]


def selectors_from_added_lines(diff: str) -> list[str]:
    """Component selectors declared in newly-added lines (catches new @Component decorators)."""
    selectors: list[str] = []
    for line in added_lines(diff):
        match = _SELECTOR_RE.search(line)
        if match:
            selectors.append(match.group(1))
    return selectors


def selector_for_component_file(component_ts_path: Path) -> str | None:
    """Read a *.component.ts file and pull its `selector` from the @Component decorator."""
    if not component_ts_path.exists():
        return None
    text = component_ts_path.read_text(errors='replace')
    match = _SELECTOR_RE.search(text)
    return match.group(1) if match else None


def data_testids_in_added_lines(diff: str) -> list[str]:
    """Distinct data-testid values introduced by `+` lines."""
    seen: list[str] = []
    for line in added_lines(diff):
        for match in _DATA_TESTID_RE.finditer(line):
            value = match.group(1)
            if value not in seen:
                seen.append(value)
    return seen


def routes_in_added_lines(diff: str) -> list[str]:
    """Route paths declared in newly-added lines (heuristic — matches `path: 'foo'`)."""
    seen: list[str] = []
    for line in added_lines(diff):
        match = _ROUTE_PATH_RE.search(line)
        if match:
            value = match.group(1)
            if value and value not in seen:
                seen.append(value)
    return seen


def compute_ui_surface_delta(diff: str, repo_root: Path | None = None) -> UISurfaceDelta:
    """Combine all surface signals from a single diff.

    If `repo_root` is provided, also reads each new *.component.ts file and pulls its
    `selector` from the file (covers the case where the @Component decorator existed
    in a prior commit on the branch and isn't in the current PR's diff).
    """
    new_components = tuple(added_files_in_diff(diff, '.component.ts'))
    selectors_in_diff = list(selectors_from_added_lines(diff))

    if repo_root is not None:
        for path in new_components:
            from_file = selector_for_component_file(repo_root / path)
            if from_file and from_file not in selectors_in_diff:
                selectors_in_diff.append(from_file)

    return UISurfaceDelta(
        new_component_files=new_components,
        new_component_selectors=tuple(selectors_in_diff),
        new_data_testids=tuple(data_testids_in_added_lines(diff)),
        new_route_paths=tuple(routes_in_added_lines(diff)),
    )
