"""E2E coverage self-review — heuristic check + verdict.

Step 3 of the v6p0.5 initiative cluster (`standards-require-e2e-and-ui-coverage-per-init`).

The agent should treat e2e-test extension as a hard rule on any behaviour-changing
initiative, not a reactive nice-to-have. Step 1 (`end2end_gate.py`) gave us a
*post-hoc* parser for what the deployed gates reported; this module is the
*pre-flight* counterpart that runs **before** the agent pushes the PR.

## What it does

Given the PR's unified diff, decide whether the diff introduces new behaviour
that should be covered by ``scripts/e2e.sh`` (backend) or ``scripts/e2e-ui.sh``
(UI). The decision is a heuristic — over-cautious by design, with a documented
escape hatch (operator posts ``/skip-e2e-check`` as a PR comment).

The single public entry point is :func:`evaluate_e2e_coverage`, which returns a
:class:`E2ECoverageVerdict` whose ``action`` is ``halt`` or ``proceed`` plus a
human-readable explanation. The agent's pre-push self-review step consumes that
verdict; the calibration prompt (in :mod:`gate.agent.initiative_prompt`) tells
the agent to halt-and-extend when ``action == 'halt'``.

## Heuristics — backend

A diff introduces *new backend behaviour* when any added line in a Python file
matches:

- ``@app.<method>(`` / ``@router.<method>(`` — FastAPI / Starlette decorators
- ``add_api_route(`` — programmatic FastAPI registration
- ``@<click_group>.command(`` — new CLI commands (also user-facing flows)

…or in a Go file matches:

- ``router.<Method>(`` — gin/echo/chi style
- ``mux.Handle(`` / ``mux.HandleFunc(``
- ``http.HandleFunc(``

The patterns intentionally over-fire. A pure refactor that re-indents an
existing decorator will not match (the ``@app.get(`` line was unchanged and
therefore not in the ``+`` line set).

## Heuristics — UI

A diff introduces *new UI surface* when :func:`gate.tools.ui_surface_diff.compute_ui_surface_delta`
returns a non-empty delta. That helper already detects new components, new
``data-testid`` anchors, and new routes — exactly the surface a Playwright
spec needs to assert against.

## Coverage detection

E2E coverage is considered *extended* iff the diff also touches:

- ``scripts/e2e.sh`` (backend coverage), OR
- ``scripts/e2e-ui.sh`` (UI coverage), OR
- A Playwright spec under ``end2end-ui/*.spec.ts`` or ``e2e/*.spec.ts``
  (an acceptable alternative when the repo uses a per-spec layout)

The UI/backend dimensions are independent: a UI repo that adds a new
backend-facing flow must extend BOTH ``e2e.sh`` AND ``e2e-ui.sh``.

## Bypass

The PR-comment escape (``/skip-e2e-check``) is honoured iff its author is a
human (not the agent itself). The verdict records who issued the bypass and
the comment ID for auditability.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from gate.tools.pr_diff import added_files
from gate.tools.ui_surface_diff import UISurfaceDelta, compute_ui_surface_delta

logger = logging.getLogger(__name__)


# ─── Heuristic regexes (over-cautious by design) ─────────────────────────────


# FastAPI / Starlette decorator-style route registration.
# Matches: @app.get(  @router.post(  @api_v2.delete(  @v1.head(
_PY_DECORATOR_RE: Final = re.compile(r'@\w+\.(get|post|put|patch|delete|head|options|websocket|route)\s*\(')

# FastAPI programmatic registration:  app.add_api_route(...)  router.add_api_route(...)
_PY_ADD_ROUTE_RE: Final = re.compile(r'\.add_api_route\s*\(')

# Click CLI commands (also user-facing): @cli.command(  @group.command(
_PY_CLI_COMMAND_RE: Final = re.compile(r'@\w+\.command\s*\(')

# Go HTTP handlers — gin/echo/chi/net-http
_GO_ROUTER_RE: Final = re.compile(
    r'\b(?:router|r|engine|mux|app|api)\.(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Handle|HandleFunc)\s*\('
)
_GO_HTTP_HANDLE_RE: Final = re.compile(r'\bhttp\.(?:HandleFunc|Handle)\s*\(')


# Files whose presence in the diff counts as "e2e coverage extended".
_BACKEND_COVERAGE_FILES: Final = frozenset(
    {
        'scripts/e2e.sh',
        'scripts/e2e.sh.in',  # template variant some repos use
    }
)
_UI_COVERAGE_FILES: Final = frozenset(
    {
        'scripts/e2e-ui.sh',
    }
)


# Playwright specs anywhere under `end2end-ui/` or `e2e/` directories.
_PLAYWRIGHT_SPEC_RE: Final = re.compile(r'(^|/)(end2end-ui|e2e)/.*\.spec\.ts$')


# ─── Diff inspection helpers ─────────────────────────────────────────────────


def _python_files_in_diff(diff: str) -> list[str]:
    return added_files(diff, pattern='.py')


def _go_files_in_diff(diff: str) -> list[str]:
    return added_files(diff, pattern='.go')


def _file_added_lines(diff: str, path: str) -> list[str]:
    """Return added lines (without the leading ``+``) for one file in a diff.

    This is intentionally lightweight — we walk the diff hunk-by-hunk and emit
    every ``+`` line whose enclosing ``diff --git`` block points at ``path``.
    Diff headers (``+++ b/...``) are excluded.
    """
    out: list[str] = []
    in_file = False
    target_marker = f'+++ b/{path}'
    for line in diff.splitlines():
        if line.startswith('diff --git '):
            in_file = False
            continue
        if line.startswith('+++ b/'):
            in_file = line == target_marker
            continue
        if not in_file:
            continue
        if line.startswith('+') and not line.startswith('+++'):
            out.append(line[1:])
    return out


def _is_likely_real_python_decorator(stripped: str) -> bool:
    """A real Python decorator line BEGINS with ``@``.

    Docstring / comment examples that mention ``@app.get(`` embedded mid-text
    would false-positive the bare regex. We require the decorator to be at
    column 0 (after whitespace) so prose references are skipped.
    """
    return stripped.startswith('@') and _PY_DECORATOR_RE.match(stripped) is not None


def _is_likely_cli_command(stripped: str) -> bool:
    """Mirror of :func:`_is_likely_real_python_decorator` for ``@cli.command(``."""
    return stripped.startswith('@') and _PY_CLI_COMMAND_RE.match(stripped) is not None


def detect_new_backend_endpoints(diff: str) -> list[str]:
    """Return distinct match snippets indicating new backend endpoints.

    Each entry is the *first ~80 chars* of a matching added line, useful for
    explaining the verdict to the agent. Order matches the diff order.

    Important: a real Python decorator BEGINS with ``@`` at the start of its
    (stripped) line. Docstring / comment references that embed ``@app.get(``
    mid-sentence are NOT real endpoint registrations and must not trigger the
    halt. ``add_api_route`` is treated as programmatic registration and may
    appear anywhere on the line, but is still skipped when on a comment.
    """
    hits: list[str] = []
    for path in _python_files_in_diff(diff):
        # Skip the e2e script + test files — adding routes to a test isn't
        # introducing public behaviour.
        if path.startswith('tests/') or path.endswith('_test.py'):
            continue
        for line in _file_added_lines(diff, path):
            stripped = line.lstrip()
            # Skip Python comments — `# @router.get(...)` is documentation, not code.
            if stripped.startswith('#'):
                continue
            if _is_likely_real_python_decorator(stripped) or _is_likely_cli_command(stripped):
                hits.append(stripped[:80])
                continue
            # `app.add_api_route(...)` may appear anywhere on the line (not a decorator).
            if _PY_ADD_ROUTE_RE.search(stripped):
                hits.append(stripped[:80])
    for path in _go_files_in_diff(diff):
        if path.endswith('_test.go'):
            continue
        for line in _file_added_lines(diff, path):
            stripped = line.lstrip()
            # Skip Go comments.
            if stripped.startswith('//'):
                continue
            if _GO_ROUTER_RE.search(stripped) or _GO_HTTP_HANDLE_RE.search(stripped):
                hits.append(stripped[:80])
    # De-dup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _is_test_path(path: str) -> bool:
    """Return True for paths that hold test fixtures rather than shipped code.

    Covers Python's ``tests/`` + ``*_test.py``, Go's ``*_test.go``, and
    TypeScript ``*.spec.ts`` / ``*.test.ts``. We treat fixtures inside test
    files as out-of-band for the new-surface heuristic so a unit test that
    embeds a sample diff (or a sample Angular component) doesn't trigger a
    halt on the PR that adds the test.
    """
    return (
        path.startswith('tests/')
        or '/tests/' in path
        or path.endswith('_test.py')
        or path.endswith('_test.go')
        or path.endswith('.spec.ts')
        or path.endswith('.test.ts')
        or path.endswith('.spec.tsx')
        or path.endswith('.test.tsx')
    )


def _strip_test_files_from_diff(diff: str) -> str:
    """Return a diff with every test-file hunk removed.

    Walks the unified diff hunk-by-hunk (each hunk starts at ``diff --git``)
    and drops the ones whose target path is a test file. Used to keep UI
    surface detection from latching onto fixture content inside the unit
    tests that pin this module.
    """
    out: list[str] = []
    keep = True
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith('diff --git '):
            # ``diff --git a/<path> b/<path>`` — the trailing path is the new one.
            parts = line.split(' b/', 1)
            current_path = parts[1].strip() if len(parts) == 2 else None
            keep = not (_is_test_path(current_path) if current_path else False)
        if keep:
            out.append(line)
    return '\n'.join(out)


def detect_new_ui_surface(diff: str) -> UISurfaceDelta:
    """Detect new UI surface in a diff, ignoring test-file fixtures.

    Strips ``tests/*`` / ``*_test.py`` / ``*.spec.ts`` hunks before delegating
    to :func:`compute_ui_surface_delta` so a unit test that embeds a sample
    Angular diff doesn't false-positive into "new screen added".
    """
    return compute_ui_surface_delta(_strip_test_files_from_diff(diff))


def detect_backend_e2e_extension(diff: str) -> bool:
    """True iff the diff modifies ``scripts/e2e.sh`` (or a recognised variant)."""
    return any(path in _BACKEND_COVERAGE_FILES for path in added_files(diff))


def detect_ui_e2e_extension(diff: str) -> bool:
    """True iff the diff modifies ``scripts/e2e-ui.sh`` or a Playwright spec."""
    files = added_files(diff)
    if any(path in _UI_COVERAGE_FILES for path in files):
        return True
    return any(_PLAYWRIGHT_SPEC_RE.search(path) for path in files)


# ─── Bypass detection ────────────────────────────────────────────────────────


# A PR comment is the bypass channel. The escape is recognised only when the
# comment body is *exactly* ``/skip-e2e-check`` (optionally with trailing reason
# text on the same line) AND the author is NOT the agent's own GitHub login.
# We pass in the comment list rather than fetching here so the function stays
# pure-by-default; the caller (run_driver / initiative.py) is responsible for
# the gh-api call.
_SKIP_DIRECTIVE_RE: Final = re.compile(r'^\s*/skip-e2e-check\b(?:\s+(?P<reason>.+?))?\s*$', re.MULTILINE)

# Logins we treat as the agent itself. The bypass is a HUMAN override; the
# agent must never bypass its own check.
AGENT_LOGINS: Final = frozenset(
    {
        'leartech-automated-agent',
        'leartech-automated-agent[bot]',
        'github-actions[bot]',  # safety net — bot accounts should never bypass
    }
)


@dataclass(frozen=True)
class SkipDirective:
    """A recognised ``/skip-e2e-check`` bypass."""

    actor: str  # GitHub login of the comment author
    comment_id: int  # GitHub comment ID for audit traceability
    reason: str | None = None  # optional free-text reason on the same line


def find_skip_directive(comments: list[dict[str, Any]]) -> SkipDirective | None:
    """Scan PR comments for a ``/skip-e2e-check`` directive.

    ``comments`` is the raw shape returned by ``gh api repos/<r>/issues/<n>/comments``
    — a list of dicts each with at least ``id``, ``body``, and ``user.login`` keys.
    Returns the *most recent* matching directive whose author is NOT one of
    :data:`AGENT_LOGINS`. Returns ``None`` if no matching directive exists.

    Comments are scanned in order; the last match wins so a follow-up comment
    that re-issues the bypass after a rebase still counts.
    """
    found: SkipDirective | None = None
    for c in comments:
        if not isinstance(c, dict):
            continue
        author = ((c.get('user') or {}).get('login') or '').strip()
        if not author or author in AGENT_LOGINS:
            continue
        body = c.get('body') or ''
        match = _SKIP_DIRECTIVE_RE.search(body)
        if match is None:
            continue
        try:
            cid = int(c.get('id') or 0)
        except (TypeError, ValueError):
            cid = 0
        reason = match.group('reason')
        reason = reason.strip() if reason else None
        found = SkipDirective(actor=author, comment_id=cid, reason=reason)
    return found


# ─── Verdict ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class E2ECoverageVerdict:
    """Pre-push self-review verdict.

    Shape:

        - ``action``: ``halt`` (agent must NOT push until e2e extended) or
          ``proceed`` (agent may push)
        - ``reasons``: ordered list of human-readable explanations
        - ``new_backend_endpoints``: hits flagged by the heuristic
        - ``new_ui_surface``: the UI delta (may be empty)
        - ``backend_covered``: True iff scripts/e2e.sh was extended
        - ``ui_covered``: True iff scripts/e2e-ui.sh or a Playwright spec was extended
        - ``bypass``: the matching ``SkipDirective`` when present
    """

    action: str  # 'halt' | 'proceed'
    reasons: tuple[str, ...] = field(default_factory=tuple)
    new_backend_endpoints: tuple[str, ...] = field(default_factory=tuple)
    new_ui_surface: UISurfaceDelta = field(default_factory=UISurfaceDelta)
    backend_covered: bool = False
    ui_covered: bool = False
    bypass: SkipDirective | None = None

    @property
    def is_halt(self) -> bool:
        return self.action == 'halt'

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form for logging / audit trail."""
        return {
            'kind': 'e2e_coverage_verdict',
            'action': self.action,
            'reasons': list(self.reasons),
            'new_backend_endpoints': list(self.new_backend_endpoints),
            'new_ui_surface': {
                'new_component_files': list(self.new_ui_surface.new_component_files),
                'new_component_selectors': list(self.new_ui_surface.new_component_selectors),
                'new_data_testids': list(self.new_ui_surface.new_data_testids),
                'new_route_paths': list(self.new_ui_surface.new_route_paths),
            },
            'backend_covered': self.backend_covered,
            'ui_covered': self.ui_covered,
            'bypass': (
                None
                if self.bypass is None
                else {
                    'actor': self.bypass.actor,
                    'comment_id': self.bypass.comment_id,
                    'reason': self.bypass.reason,
                }
            ),
        }


def evaluate_e2e_coverage(
    *,
    diff: str,
    comments: list[dict[str, Any]] | None = None,
) -> E2ECoverageVerdict:
    """Decide whether the agent should halt and extend e2e coverage before pushing.

    Inputs:
        diff: the unified diff of the PR (or the staged changes) as a single string.
        comments: optional PR comments (raw gh-api shape) used to detect a
            ``/skip-e2e-check`` bypass. If omitted, no bypass is recognised.

    Rules:
        1. If a non-agent commenter posted ``/skip-e2e-check``, return ``proceed``
           with a ``bypass`` recording the actor + comment id (auditability).
        2. Otherwise scan the diff:
            - new backend behaviour + no ``scripts/e2e.sh`` change → ``halt``
            - new UI surface + no UI coverage (e2e-ui.sh OR Playwright spec) → ``halt``
            - both: each dimension is checked independently
        3. If neither new-backend nor new-UI surface is detected → ``proceed``.

    The function is pure — no I/O, no subprocess, no GitHub calls. Callers
    that want to honour the bypass pass ``comments`` from ``gh api ...``.
    """
    bypass = find_skip_directive(comments or [])
    new_backend = tuple(detect_new_backend_endpoints(diff))
    new_ui = detect_new_ui_surface(diff)
    backend_covered = detect_backend_e2e_extension(diff)
    ui_covered = detect_ui_e2e_extension(diff)

    if bypass is not None:
        return E2ECoverageVerdict(
            action='proceed',
            reasons=(f'/skip-e2e-check bypass honoured (actor={bypass.actor}, comment_id={bypass.comment_id})',),
            new_backend_endpoints=new_backend,
            new_ui_surface=new_ui,
            backend_covered=backend_covered,
            ui_covered=ui_covered,
            bypass=bypass,
        )

    reasons: list[str] = []
    if new_backend and not backend_covered:
        reasons.append(
            f'Diff introduces {len(new_backend)} new backend endpoint(s) but '
            f'`scripts/e2e.sh` was not extended. Add at least one scenario that '
            f'exercises the new behaviour.'
        )
    if not new_ui.is_empty and not ui_covered:
        bits: list[str] = []
        if new_ui.new_component_selectors:
            bits.append(f'{len(new_ui.new_component_selectors)} new component selector(s)')
        if new_ui.new_route_paths:
            bits.append(f'{len(new_ui.new_route_paths)} new route(s)')
        if new_ui.new_data_testids:
            bits.append(f'{len(new_ui.new_data_testids)} new data-testid anchor(s)')
        reasons.append(
            'Diff introduces new UI surface (' + ', '.join(bits) + ') but neither '
            '`scripts/e2e-ui.sh` nor any `end2end-ui/*.spec.ts` Playwright spec was '
            'extended. Add a Playwright spec that asserts against the new surface.'
        )

    if reasons:
        return E2ECoverageVerdict(
            action='halt',
            reasons=tuple(reasons),
            new_backend_endpoints=new_backend,
            new_ui_surface=new_ui,
            backend_covered=backend_covered,
            ui_covered=ui_covered,
            bypass=None,
        )

    # Either no new behaviour, or every dimension is covered.
    proceed_reasons: list[str] = []
    if new_backend and backend_covered:
        proceed_reasons.append(
            f'Backend coverage extended ({len(new_backend)} new endpoint(s); `scripts/e2e.sh` modified).'
        )
    if not new_ui.is_empty and ui_covered:
        proceed_reasons.append('UI coverage extended (Playwright spec or `scripts/e2e-ui.sh` modified).')
    if not proceed_reasons:
        proceed_reasons.append(
            'No new endpoints, CLI commands, or UI surface detected — pure refactor / docs / config.'
        )

    return E2ECoverageVerdict(
        action='proceed',
        reasons=tuple(proceed_reasons),
        new_backend_endpoints=new_backend,
        new_ui_surface=new_ui,
        backend_covered=backend_covered,
        ui_covered=ui_covered,
        bypass=None,
    )


__all__ = [
    'AGENT_LOGINS',
    'E2ECoverageVerdict',
    'SkipDirective',
    'detect_backend_e2e_extension',
    'detect_new_backend_endpoints',
    'detect_new_ui_surface',
    'detect_ui_e2e_extension',
    'evaluate_e2e_coverage',
    'find_skip_directive',
]
