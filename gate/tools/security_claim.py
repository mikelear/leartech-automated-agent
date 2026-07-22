"""Security-claim-in-diff audit — the paper trail behind "protected by X in production".

When a PR introduces a comment or documentation line asserting that a
security-sensitive feature is "protected by X in production" — where X is a
NetworkPolicy, auth middleware, RBAC binding, or similar guard — the criterion
this module powers requires evidence that X actually exists. Either:

  (a) the diff itself introduces the guard (a Kubernetes manifest of the
      matching kind, an auth-middleware wire-up, an RBAC role binding), OR
  (b) the claim's surrounding text, PR title, or PR body references an existing
      manifest whose presence can be verified at the PR's head SHA.

Motivation — PR #61-style regression
-------------------------------------
A feature flip that enabled Hydra's ``/oauth2/register`` endpoint carried a
chart-comment claiming NetworkPolicy protection. The NetworkPolicy was
neither introduced in that PR nor referenced by any path the reviewer could
verify. The claim was structurally untrue at merge-time; only later did
someone notice the endpoint was reachable from off-cluster.

This module turns that latent claim → gap into a PR-time failure so the
reviewer sees the missing guard immediately, not after the change ships.

The module deliberately splits pure parsing from I/O — the parsers are unit
tested without touching the network, and the single ``gh``-shelling seam
(:func:`manifest_exists_at_ref`) is monkeypatched in tests.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# Bounded ``gh`` invocation ceiling. A stalled ``gh api`` call must not wedge
# the gate pipeline — 30s matches the sibling chart_overlay tool.
_GH_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Claim detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityClaim:
    """A security-sensitive claim discovered in an added comment/doc line."""

    source_file: str  # e.g. "charts/leartech-hydra/values.yaml"
    claim_type: str  # 'network_policy' | 'auth' | 'rbac'
    claim_snippet: str  # the exact regex match — "protected by NetworkPolicy"
    context_line: str  # the full comment/doc line, trimmed


# Regex patterns per guard kind. Each ``(claim_type, pattern)`` pair is
# consulted in order; the first match on a given line wins so one claim can't
# be double-counted across types. Patterns are deliberately conservative —
# they require an explicit "protected/restricted/behind/guarded/etc." verb
# adjacent to the guard noun to avoid firing on incidental prose ("we should
# add a NetworkPolicy someday" is NOT a claim).
_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ---- NetworkPolicy ----
    (
        'network_policy',
        re.compile(
            r'(?:protected|restricted|gated|guarded|blocked|filtered|shielded)\s+'
            r'(?:by|via|behind|with)\s+(?:a\s+|the\s+|our\s+)?network[- ]?polic(?:y|ies)',
            re.IGNORECASE,
        ),
    ),
    (
        'network_policy',
        re.compile(
            r'behind\s+(?:a\s+|the\s+|our\s+)?network[- ]?polic(?:y|ies)',
            re.IGNORECASE,
        ),
    ),
    (
        'network_policy',
        re.compile(
            r'network[- ]?polic(?:y|ies)\s+(?:restrict|protect|guard|block|gate|filter)',
            re.IGNORECASE,
        ),
    ),
    # ---- Auth middleware ----
    (
        'auth',
        re.compile(
            r'(?:protected|restricted|gated|guarded|behind)\s+'
            r'(?:by|via|behind|with)\s+'
            r'(?:the\s+|our\s+)?(?:auth|authentication)\s+middleware',
            re.IGNORECASE,
        ),
    ),
    (
        'auth',
        re.compile(
            r'(?:requires|behind|guarded\s+by)\s+'
            r'(?:the\s+|our\s+)?(?:auth|authentication)\s+middleware',
            re.IGNORECASE,
        ),
    ),
    # ---- RBAC ----
    (
        'rbac',
        re.compile(
            r'(?:protected|restricted|gated|guarded|behind)\s+'
            r'(?:by|via|behind|with)\s+RBAC',
            re.IGNORECASE,
        ),
    ),
    (
        'rbac',
        re.compile(
            r'RBAC[- ]?(?:restricted|protected|gated|guarded)',
            re.IGNORECASE,
        ),
    ),
)


# Comment-shaped prefixes we recognise across the languages this repo touches.
# YAML/Python: ``#``; Go/TS/JS/tpl: ``//`` or ``*`` (inside ``/* */``);
# SQL: ``--``. Doc files (`.md`, `.rst`, `.txt`, `.adoc`) are always in-scope
# regardless of prefix, since their entire content is documentation.
_COMMENT_PREFIXES = ('#', '//', '--', '*')
_DOC_FILE_SUFFIXES = ('.md', '.rst', '.txt', '.adoc')


def _is_comment_line(text: str) -> bool:
    """Return True if the given text (already stripped of the diff `+`) looks like a comment."""
    stripped = text.lstrip()
    if not stripped:
        return False
    return stripped.startswith(_COMMENT_PREFIXES)


def _is_doc_file(path: str) -> bool:
    return path.endswith(_DOC_FILE_SUFFIXES)


def _iter_added_lines_by_file(diff: str) -> list[tuple[str, str]]:
    """Yield ``(path, added_line_text)`` for every ``+``-prefixed line in the diff.

    Skips the ``+++ b/…`` header lines and any lines outside a file block.
    """
    result: list[tuple[str, str]] = []
    current_path: str | None = None
    for raw in diff.splitlines():
        if raw.startswith('diff --git'):
            current_path = None
            continue
        if raw.startswith('+++ b/'):
            current_path = raw[6:]
            continue
        if raw.startswith('--- '):
            continue
        if not current_path:
            continue
        if raw.startswith('@@'):
            continue
        if raw.startswith('+') and not raw.startswith('+++'):
            result.append((current_path, raw[1:]))
    return result


def parse_security_claims(diff: str) -> list[SecurityClaim]:
    """Scan a unified diff for security-sensitive protection claims.

    Only considers **added** lines that appear to be comments (or lines in
    dedicated doc files). Non-comment code lines are ignored: the assertion
    "protected by NetworkPolicy" appearing inside a string literal or a test
    fixture is not a claim being made *by* the author about their production
    posture — it's data. Comment-shape is the precision-boosting filter.
    """
    claims: list[SecurityClaim] = []
    for path, text in _iter_added_lines_by_file(diff):
        if not (_is_doc_file(path) or _is_comment_line(text)):
            continue
        for claim_type, pat in _CLAIM_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            claims.append(
                SecurityClaim(
                    source_file=path,
                    claim_type=claim_type,
                    claim_snippet=m.group(0),
                    context_line=text.strip(),
                )
            )
            break  # one claim per line — first-matching type wins
    return claims


# ---------------------------------------------------------------------------
# In-diff evidence — is the guard itself present in this PR's changes?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EvidencePattern:
    """A pattern for locating guard evidence within a diff.

    ``target`` picks whether the pattern is matched against a diff file's PATH
    (e.g. detecting that a manifest file named ``networkpolicy.yaml`` was
    added) or the CONTENT of an added line (e.g. detecting a ``kind:
    NetworkPolicy`` declaration).
    """

    target: str  # 'path' | 'content'
    pattern: re.Pattern[str]


# One matching pattern is enough — patterns aren't mutually exclusive.
_IN_DIFF_EVIDENCE: dict[str, tuple[_EvidencePattern, ...]] = {
    'network_policy': (
        _EvidencePattern('content', re.compile(r'^\s*kind:\s*NetworkPolicy\b')),
        _EvidencePattern(
            'path',
            re.compile(r'(?:^|/)(?:network[- ]?polic(?:y|ies))[a-zA-Z0-9_.-]*\.(?:ya?ml|tpl)$', re.IGNORECASE),
        ),
    ),
    'auth': (
        _EvidencePattern('content', re.compile(r'\badd_middleware\s*\(\s*[A-Za-z_.]*Authentication')),
        _EvidencePattern(
            'content',
            re.compile(r'\bDepends\s*\(\s*(?:get_current_user|require_auth|authenticated_user|require_user)\b'),
        ),
        _EvidencePattern('content', re.compile(r'\brequire[_-]?auth\s*\(')),
    ),
    'rbac': (
        _EvidencePattern('content', re.compile(r'^\s*kind:\s*(?:Cluster)?Role(?:Binding)?\b')),
        _EvidencePattern(
            'path',
            re.compile(
                r'(?:^|/)(?:role|rolebinding|clusterrole|clusterrolebinding)[a-zA-Z0-9_.-]*\.(?:ya?ml|tpl)$',
                re.IGNORECASE,
            ),
        ),
    ),
}


def has_in_diff_evidence(diff: str, claim_type: str) -> str:
    """Return a short reason string if the diff introduces a guard of ``claim_type``.

    Empty string means no matching evidence in the diff. The reason names the
    file where the guard was found so the criterion's failure message can
    point at the concrete artefact.
    """
    patterns = _IN_DIFF_EVIDENCE.get(claim_type, ())
    if not patterns:
        return ''
    for path, text in _iter_added_lines_by_file(diff):
        for ep in patterns:
            target = path if ep.target == 'path' else text
            if ep.pattern.search(target):
                return f'{ep.target} match in {path}'
    return ''


# ---------------------------------------------------------------------------
# Referenced-manifest detection (pure)
# ---------------------------------------------------------------------------


# Repo-relative paths ending in one of the file extensions we care about,
# with at least one directory segment. Requiring a slash rules out bare
# filename mentions ("foo.yaml") which are ambiguous — a real reference in a
# code comment or PR body virtually always includes a directory hint
# (`charts/foo/templates/networkpolicy.yaml`, `helmfiles/jx-prod/...`, etc.).
_MANIFEST_REF_RE = re.compile(
    r'(?<![A-Za-z0-9_./\\])'  # left-boundary — don't match mid-word
    r'([A-Za-z0-9_.-]+/[A-Za-z0-9_./\-]+\.(?:ya?ml|py|go|ts|tsx|js|tpl))'
    r'\b'
)


def find_manifest_refs(*texts: str) -> list[str]:
    """Extract repo-relative manifest paths referenced in the given texts.

    Returned in first-seen order, de-duplicated. Empty inputs are tolerated.
    """
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for m in _MANIFEST_REF_RE.finditer(text):
            path = m.group(1)
            if path not in found:
                found.append(path)
    return found


# ---------------------------------------------------------------------------
# Manifest existence lookup — I/O; mockable via monkeypatching this module
# ---------------------------------------------------------------------------


def _gh(args: list[str]) -> str:
    """Invoke ``gh`` with a hard wall-clock ceiling — RuntimeError on failure/timeout.

    Callers translate BOTH non-zero exit and timeout into "manifest not found"
    (an inaccessible existence check is indistinguishable from absence for
    the criterion's verdict purposes).
    """
    try:
        result = subprocess.run(  # noqa: S603 - argv comes from typed callers only
            ['gh', *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'gh {" ".join(args)} timed out after {_GH_TIMEOUT_SECONDS}s') from e
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def manifest_exists_at_ref(repo: str, path: str, ref: str = 'main') -> bool:
    """Does ``path`` exist as a file in ``repo`` at ``ref``?

    Any failure — network error, 404, non-zero ``gh`` exit, timeout, or the
    contents endpoint returning something other than a file — folds into
    ``False``. The criterion's semantic is "we could verify the guard is
    there"; anything short of a confirmed hit is a miss.
    """
    try:
        raw = _gh(['api', f'repos/{repo}/contents/{path}?ref={ref}', '--jq', '.type'])
    except RuntimeError:
        return False
    return raw.strip().strip('"') == 'file'


# ---------------------------------------------------------------------------
# Composed verdict — the public seam the criterion consumes
# ---------------------------------------------------------------------------


def evidence_for_claim(
    claim: SecurityClaim,
    diff: str,
    pr_title: str,
    pr_body: str,
    repo: str,
    head_sha: str,
) -> tuple[bool, str]:
    """Combined verdict: does the claim have in-diff or referenced-manifest evidence?

    Returns ``(ok, reason)``:
      * ``ok=True``: either the diff itself introduces a matching guard, OR a
        manifest referenced in the claim's context / PR title / PR body was
        confirmed to exist at ``head_sha``.
      * ``ok=False``: neither in-diff evidence nor a verifiable referenced
        manifest was found. ``reason`` describes the gap concretely so the
        reviewer can act on it.
    """
    in_diff = has_in_diff_evidence(diff, claim.claim_type)
    if in_diff:
        return True, f'{claim.claim_type} guard present in diff ({in_diff})'

    refs = find_manifest_refs(claim.context_line, pr_title, pr_body)
    verified: list[str] = []
    for ref in refs:
        if manifest_exists_at_ref(repo, ref, head_sha):
            verified.append(ref)
    if verified:
        return True, 'referenced manifest(s) exist at head SHA: ' + ', '.join(verified)

    if refs:
        return (
            False,
            (
                f'{claim.claim_type} claim in {claim.source_file} '
                f'({claim.context_line!r}) references {refs} but none exist at '
                f'head SHA {head_sha[:7]}. Either add the guard in this PR or '
                f'reference an existing manifest path.'
            ),
        )
    return (
        False,
        (
            f'{claim.claim_type} claim in {claim.source_file} '
            f'({claim.context_line!r}) has no matching guard in the diff and '
            f'references no manifest path. Either add the guard in this PR '
            f'(e.g. a NetworkPolicy manifest, auth middleware wire-up, or RBAC '
            f'binding) OR cite an existing manifest path (e.g. '
            f'`charts/<chart>/templates/networkpolicy.yaml`) in a nearby '
            f'comment or the PR body.'
        ),
    )
