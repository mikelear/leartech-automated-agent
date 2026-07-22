"""Chart-flip → overlay-PR audit trail.

When a Helm chart's ``values.yaml`` introduces a NEW toggle set to a "safe"
default (``foo.enabled: false``) whose adjacent comment claims that a
per-cluster GitOps overlay flips it on, the criterion this module powers
requires evidence that the overlay PR actually exists — either:

  (a) the current overlay YAML at ``mikelear/jx-build-cluster-{gsm,akv}``
      already contains the matching key set to a truthy override, OR
  (b) the PR title/body references a linked overlay PR by ``owner/repo#N``
      or a URL to the cluster GitOps repo.

Motivation: agent-authored PRs have shipped brand-new ``*.enabled: false``
toggles with confident chart-comments claiming a "prod overlay opts in" —
without a paired overlay PR actually landing. In production the toggle
stays off; the feature never ships. This tool turns that latent gap into a
loud PR-time failure.

The module deliberately splits pure parsing from I/O so unit tests can
exercise the detection logic without hitting the network.
"""

from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from typing import Any

import yaml

# Known per-cluster GitOps overlay repositories. Each pair is
# (cluster_key, owner/repo). The overlay YAML path convention is
# ``helmfiles/<env>/configs/<chart>.yaml`` — same across both clusters.
CLUSTER_OVERLAY_REPOS: dict[str, str] = {
    'gcp': 'mikelear/jx-build-cluster-gsm',
    'az': 'mikelear/jx-build-cluster-akv',
}

# Environments we treat as production-adjacent. If the flip is expected to be
# opted-into on any of these, we check the overlay YAML there.
DEFAULT_OVERLAY_ENVS: tuple[str, ...] = ('jx-staging', 'jx-production')


# Regex patterns applied against the comment block preceding (or trailing) a
# ``*.enabled: false`` value to decide "does this line's author claim a prod
# overlay will flip this on?". Case-insensitive.
#
# Deliberately conservative: we want high precision (few false positives) at
# the cost of recall. Comments that ONLY document a preview-mode default
# ("flip off in preview") without claiming a prod override should NOT match.
_OVERLAY_HINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'set\s+true\s+.*(?:via|in)\s+configs?/', re.IGNORECASE),
    re.compile(r'flip\s+(?:via|on|true)\b.*configs?/', re.IGNORECASE),
    re.compile(r'set\s+true\s+(?:on|in)\s+(?:az|gcp|prod|production|staging)\b', re.IGNORECASE),
    re.compile(r'override(?:d|s|n)?\s+.*(?:in|on|via)\s+.*(?:prod|production|overlay|gitops)', re.IGNORECASE),
    re.compile(r'flipped?\s+on\s+.*(?:in|via)\s+.*(?:prod|production|overlay|gitops|cluster)', re.IGNORECASE),
    re.compile(r'enabled\s+.*(?:in|via)\s+.*(?:prod|production|overlay|gitops)\b', re.IGNORECASE),
    re.compile(r'opt(?:ed|s)?\s+in\s+.*(?:prod|production|overlay|gitops)\b', re.IGNORECASE),
)


@dataclass(frozen=True)
class ChartFlipSignal:
    """A newly-added ``*.enabled: false`` line whose comment claims a prod overlay flip."""

    chart_path: str  # e.g. "charts/leartech-automated-agent/values.yaml"
    chart_name: str  # e.g. "leartech-automated-agent"
    dotted_key: str  # e.g. "postgresql.enabled" or "dcr.enabled"
    default_value: bool  # what the added line sets (True/False)
    hint_snippet: str  # the comment fragment that matched an overlay-hint pattern

    def matches_overlay_value(self, overlay_dict: dict[str, Any]) -> bool:
        """True iff `overlay_dict` (loaded from an overlay YAML) sets the dotted key
        to a value that differs from `default_value` (i.e. actually overrides it).
        """
        got = _dig(overlay_dict, self.dotted_key.split('.'))
        if got is None:
            return False
        # If default was False, any truthy override counts; if default was True,
        # any explicit False counts. In practice new toggles default False and
        # overlays flip them True — but handle the reverse for symmetry.
        return bool(got) != self.default_value


def _dig(container: Any, parts: list[str]) -> Any:
    for p in parts:
        if not isinstance(container, dict):
            return None
        if p not in container:
            return None
        container = container[p]
    return container


# ---------------------------------------------------------------------------
# Pure parsing — no I/O
# ---------------------------------------------------------------------------


_CHART_VALUES_PATH_RE = re.compile(r'^charts/([^/]+)/values\.yaml$')


def _chart_name_from_path(path: str) -> str | None:
    m = _CHART_VALUES_PATH_RE.match(path)
    return m.group(1) if m else None


@dataclass(frozen=True)
class _HunkLine:
    """One line within a hunk, tagged by its diff prefix.

    ``kind`` is one of:
      * ``'+'`` for newly-added lines
      * ``' '`` for context lines (unchanged, shown for reference)
      * ``'-'`` for removed lines
    """

    kind: str  # '+' | ' ' | '-'
    text: str  # line content without the prefix


def _iter_added_values_hunks(diff: str) -> list[tuple[str, list[_HunkLine]]]:
    """Yield ``(chart_values_path, hunk_lines)`` for each hunk in a chart's values.yaml.

    Filters to files matching ``charts/*/values.yaml`` only.
    """
    result: list[tuple[str, list[_HunkLine]]] = []
    current_path: str | None = None
    current_hunk: list[_HunkLine] = []

    def flush() -> None:
        if current_path and current_hunk:
            result.append((current_path, list(current_hunk)))

    for raw in diff.splitlines():
        if raw.startswith('diff --git'):
            flush()
            current_hunk = []
            current_path = None
            continue
        if raw.startswith('+++ b/'):
            candidate = raw[6:]
            if _CHART_VALUES_PATH_RE.match(candidate):
                current_path = candidate
            else:
                current_path = None
            continue
        if raw.startswith('--- '):
            continue
        if not current_path:
            continue
        if raw.startswith('@@'):
            flush()
            current_hunk = []
            continue
        if not raw:
            continue
        head = raw[0]
        if head in ('+', '-', ' '):
            # Skip the '+++ b/…' / '--- a/…' headers which we already handled.
            if raw.startswith('+++') or raw.startswith('---'):
                continue
            current_hunk.append(_HunkLine(kind=head, text=raw[1:]))
        # Anything else (\ No newline at end of file, index …, etc.) — skip.

    flush()
    return result


_ENABLED_LINE_RE = re.compile(r'^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+)\s*:\s*(?P<val>true|false)\b')


def _dotted_key_for(hunk: list[_HunkLine], line_idx: int) -> str:
    """Compute the dotted YAML path to line_idx by walking preceding context/added lines.

    Simple stack-based indent walk. Doesn't handle sequences (lists) — chart flip
    toggles live under mappings, not lists, so we consider that acceptable.
    """
    target = hunk[line_idx]
    target_match = _ENABLED_LINE_RE.match(target.text)
    if not target_match:
        return ''
    target_indent = len(target_match.group('indent'))
    stack: list[tuple[int, str]] = []
    # Walk backwards from line before target, collecting parents.
    for i in range(line_idx - 1, -1, -1):
        line = hunk[i]
        if line.kind == '-':
            continue  # Removed line doesn't belong to the new tree.
        stripped = line.text.rstrip()
        if not stripped or stripped.lstrip().startswith('#'):
            continue
        m = re.match(r'^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+)\s*:\s*(?P<rest>.*)$', line.text)
        if not m:
            continue
        indent = len(m.group('indent'))
        key = m.group('key')
        if indent < target_indent and (not stack or indent < stack[-1][0]):
            stack.append((indent, key))
            target_indent = indent  # tighten — we only accept strictly-less indents from here up
            if indent == 0:
                break
    stack.reverse()
    parts = [k for _, k in stack] + [target_match.group('key')]
    return '.'.join(parts)


def _collect_nearby_comment_block(hunk: list[_HunkLine], line_idx: int) -> str:
    """Gather comment lines describing the target line (and its ancestor mapping keys).

    Walks backward from ``line_idx`` collecting:
      * a same-line trailing ``# …`` comment on the target itself,
      * ``#``-prefixed comment lines immediately preceding the target OR any of
        its ancestor mapping keys (skipping ancestor key lines so the comment
        block that sits ABOVE ``foo:`` still applies to ``foo.enabled: false``),

    Stops at the first of: a blank line, a removed-line marker, a sibling /
    non-ancestor non-comment line. Returns a single newline-joined string.
    """
    match = _ENABLED_LINE_RE.match(hunk[line_idx].text)
    if not match:
        return ''
    ancestor_indent = len(match.group('indent'))

    same = hunk[line_idx].text
    comments: list[str] = []
    hash_pos = same.find('#')
    if hash_pos > 0:
        comments.append(same[hash_pos + 1 :].strip())

    for i in range(line_idx - 1, -1, -1):
        line = hunk[i]
        if line.kind == '-':
            # A removed line breaks the block — the new-file view doesn't have it.
            break
        stripped = line.text.strip()
        if not stripped:
            # Blank line ends the comment block.
            break
        if stripped.startswith('#'):
            comments.append(stripped.lstrip('#').strip())
            continue
        parent = re.match(r'^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+)\s*:', line.text)
        if parent:
            indent = len(parent.group('indent'))
            if indent < ancestor_indent:
                # Ancestor mapping key — the comment block above it still
                # describes the target. Keep walking upward.
                ancestor_indent = indent
                continue
        # Sibling / non-ancestor content — stop.
        break

    return '\n'.join(reversed(comments))


def _matches_overlay_hint(comment_block: str) -> str:
    """Returns the matched hint fragment if the comment block signals an overlay flip.

    Empty string means no match.
    """
    if not comment_block:
        return ''
    for pattern in _OVERLAY_HINT_PATTERNS:
        m = pattern.search(comment_block)
        if m:
            return m.group(0)
    return ''


def parse_chart_flip_signals(diff: str) -> list[ChartFlipSignal]:
    """Scan a unified diff for newly-added ``X.enabled: <bool>`` values whose adjacent
    comment claims a per-cluster GitOps overlay flips the value in production.

    Only inspects files matching ``charts/*/values.yaml``. Only considers added
    lines (kind == ``'+'``); unchanged existing toggles are ignored.
    """
    signals: list[ChartFlipSignal] = []
    for chart_path, hunk in _iter_added_values_hunks(diff):
        chart_name = _chart_name_from_path(chart_path) or ''
        for i, line in enumerate(hunk):
            if line.kind != '+':
                continue
            m = _ENABLED_LINE_RE.match(line.text)
            if not m:
                continue
            key = m.group('key')
            if key != 'enabled':
                # Restrict to keys literally named `enabled`. This is the org
                # convention for feature toggles; broadening would explode false
                # positives (e.g. `debug: false` isn't a chart flip).
                continue
            comment_block = _collect_nearby_comment_block(hunk, i)
            hint = _matches_overlay_hint(comment_block)
            if not hint:
                continue
            dotted = _dotted_key_for(hunk, i)
            if not dotted:
                continue
            signals.append(
                ChartFlipSignal(
                    chart_path=chart_path,
                    chart_name=chart_name,
                    dotted_key=dotted,
                    default_value=(m.group('val') == 'true'),
                    hint_snippet=hint,
                )
            )
    return signals


# ---------------------------------------------------------------------------
# Linked overlay PR / URL detection (pure)
# ---------------------------------------------------------------------------


# Match any of the overlay repos by explicit `owner/repo#N` reference, or by
# raw URL to a PR on that repo.
def _overlay_pr_ref_patterns() -> list[re.Pattern[str]]:
    escaped_repos = [re.escape(r) for r in CLUSTER_OVERLAY_REPOS.values()]
    repo_alt = '|'.join(escaped_repos)
    return [
        re.compile(rf'\b(?:{repo_alt})#(?P<n>\d+)\b'),
        re.compile(rf'https?://github\.com/(?:{repo_alt})/pull/(?P<n>\d+)\b'),
    ]


def find_overlay_pr_refs(*texts: str) -> list[str]:
    """Return unique overlay-repo PR references extracted from the given texts.

    Each reference is normalised to ``owner/repo#N`` form. Both explicit
    ``mikelear/jx-build-cluster-gsm#42`` refs and full GitHub PR URLs are matched.
    """
    found: list[str] = []
    patterns = _overlay_pr_ref_patterns()
    for text in texts:
        if not text:
            continue
        for pat in patterns:
            for m in pat.finditer(text):
                whole = m.group(0)
                # Normalise URLs to `owner/repo#N`.
                if whole.startswith('http'):
                    for repo in CLUSTER_OVERLAY_REPOS.values():
                        marker = f'{repo}/pull/'
                        if marker in whole:
                            n = whole.rsplit('/', 1)[-1]
                            whole = f'{repo}#{n}'
                            break
                if whole not in found:
                    found.append(whole)
    return found


# ---------------------------------------------------------------------------
# Overlay-value lookup — I/O; mockable via monkeypatching this module
# ---------------------------------------------------------------------------


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def fetch_overlay_yaml(cluster_repo: str, path: str, ref: str = 'main') -> dict[str, Any]:
    """Fetch + parse an overlay YAML from a per-cluster GitOps repo.

    Returns an empty dict if the file is absent, unreadable, or empty.
    Does NOT raise — a missing overlay file is a first-class "no override" state.
    """
    try:
        raw = _gh(
            [
                'api',
                f'repos/{cluster_repo}/contents/{path}?ref={ref}',
                '--jq',
                '.content',
            ]
        )
    except RuntimeError:
        return {}
    if not raw.strip():
        return {}
    try:
        decoded = base64.b64decode(raw).decode('utf-8', errors='replace')
    except (ValueError, TypeError):
        return {}
    try:
        loaded = yaml.safe_load(decoded) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def any_cluster_overlay_sets_flip(
    signal: ChartFlipSignal,
    envs: tuple[str, ...] = DEFAULT_OVERLAY_ENVS,
    cluster_repos: dict[str, str] | None = None,
) -> str:
    """If any per-cluster overlay YAML sets the flip's dotted key to an override,
    return a short human-readable reason string. Empty string means no overlay found.

    We check both clusters × ``envs``; the first hit wins for reporting purposes.
    """
    repos = cluster_repos if cluster_repos is not None else CLUSTER_OVERLAY_REPOS
    for cluster, repo in repos.items():
        for env in envs:
            path = f'helmfiles/{env}/configs/{signal.chart_name}.yaml'
            overlay = fetch_overlay_yaml(repo, path)
            if not overlay:
                continue
            if signal.matches_overlay_value(overlay):
                return f'{repo}:{path} sets {signal.dotted_key} (cluster={cluster}, env={env})'
    return ''


# ---------------------------------------------------------------------------
# Composed verdict — the public seam the criterion consumes
# ---------------------------------------------------------------------------


def evidence_for_flip(
    signal: ChartFlipSignal,
    pr_title: str,
    pr_body: str,
) -> tuple[bool, str]:
    """Combined check: does this flip have evidence of a paired overlay landing?

    Returns ``(ok, reason)``:
      * ``ok=True``: an overlay YAML already sets the key, OR the PR
        references a linked overlay PR by ``owner/repo#N`` / URL. ``reason``
        describes what was found.
      * ``ok=False``: neither an overlay match nor a linked PR was found.
        ``reason`` describes the gap.
    """
    overlay_reason = any_cluster_overlay_sets_flip(signal)
    if overlay_reason:
        return True, f'overlay already sets {signal.dotted_key}: {overlay_reason}'
    refs = find_overlay_pr_refs(pr_title, pr_body)
    if refs:
        return True, 'linked overlay PR references: ' + ', '.join(refs)
    return (
        False,
        (
            f'no overlay YAML in any of {sorted(CLUSTER_OVERLAY_REPOS.values())} sets '
            f'`{signal.dotted_key}`, and PR title/body reference no overlay PR '
            f'(expected `owner/repo#N` or GitHub PR URL against a cluster repo). '
            f'Chart-comment hint: {signal.hint_snippet!r}.'
        ),
    )
