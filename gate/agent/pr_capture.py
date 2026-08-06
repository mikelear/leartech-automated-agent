"""Parse a PR number from ``gh pr create`` stdout, with ZERO prose scraping.

The birth-moment of the PR — its number — is baked into ``gh pr create``'s
stdout: a single line containing the full URL, e.g.
``https://github.com/mikelear/leartech-automated-agent/pull/42``. That URL is
BRANCH-SCOPED by construction (``gh pr create`` targets the current branch), so
parsing the number from THAT specific subprocess output is authoritative. It is
NOT a prose scrape of whatever URL the agent happens to cite mid-conversation
(how the historical ``_extract_pr_from_tool_result`` helper misfired: it matched
arbitrary GitHub PR links quoted in narrative, silently overwriting the real
number with an unrelated PR).

Sole exposed helper: :func:`parse_pr_number_from_gh_output`. Its ONLY consumer is
:mod:`gate.tools.pr_back` (the MCP admin's ``gh pr create`` subprocess wrapper).
The dev-agent loop no longer parses PR URLs at all — ``open_pr`` (MCP) records the
number from structured API JSON onto ``AgentRun.status.targetPR``, and the runtime
reads that back (``initiative._resolve_target_pr``). The old SDK-loop
capture-from-tool-result path (and its ``is_gh_pr_create_command`` classifier) was
removed with that migration.
"""

from __future__ import annotations

import re

# GitHub PR URL — matches ``https://github.com/<owner>/<repo>/pull/<n>``.
# The number capture is used by :func:`parse_pr_number_from_gh_output`. We match
# the whole shape (not just ``/pull/(\d+)``) and anchor on ``https://github.com/``
# so it scopes to a real GitHub PR link within a larger stdout blob.
PR_URL_RE = re.compile(r'https://github\.com/[^\s/]+/[^\s/]+/pull/(\d+)')


def parse_pr_number_from_gh_output(text: str) -> int | None:
    """Parse the PR number from a ``gh pr create`` stdout blob.

    Returns the integer PR number, or ``None`` when the text does not
    contain a recognisable GitHub PR URL. Whitespace + trailing
    newlines are tolerated (``gh pr create`` writes the URL followed
    by ``\\n``, so a bare ``strip()`` isn't sufficient — the regex
    handles both cases uniformly).

    Its only consumer is :mod:`gate.tools.pr_back`, which parses the URL returned
    by the MCP admin's ``gh pr create`` subprocess.
    """
    if not text:
        return None
    match = PR_URL_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        # match.group(1) is guaranteed \d+ by the regex, so ValueError
        # is unreachable in practice — but the branch keeps the return
        # type honest for type-checkers.
        return None
