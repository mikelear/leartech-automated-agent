"""Capture the PR number authoritatively at ``gh pr create`` time.

The birth-moment of the PR — its number — is baked into ``gh pr create``'s
stdout: a single line containing the full URL, e.g.
``https://github.com/mikelear/leartech-automated-agent/pull/42``. That
URL is BRANCH-SCOPED by construction — ``gh pr create`` targets the
current git branch (either the shell's ``HEAD`` or the explicit
``--head <branch>`` arg), so parsing the number from THIS specific tool
result is authoritative. It is NOT a prose scrape of whatever URL the
agent happens to cite mid-conversation (which is how the historical
``_extract_pr_from_tool_result`` helper misfired: it matched arbitrary
GitHub PR links the agent quoted in its narrative, silently overwriting
the real number with a completely unrelated PR).

Two exposed helpers:

- :func:`parse_pr_number_from_gh_output` — regex-based extractor over
  the STDOUT string of a ``gh pr create`` invocation. Returns the PR
  number, or ``None`` on miss.

- :func:`is_gh_pr_create_command` — classifier used by the SDK loop
  to decide whether a Bash tool invocation is a ``gh pr create`` call.
  We arm capture-from-tool-result ONLY for invocations this classifier
  green-lights, so we never accidentally scrape URLs out of ``gh pr
  view`` / ``gh pr comment`` / ``git log`` output.

The parse regex is the shared source-of-truth previously living inline
in :mod:`gate.tools.pr_back`; ``pr_back`` now delegates here so a
future tweak to the URL shape only needs one edit.
"""

from __future__ import annotations

import re

# GitHub PR URL — matches ``https://github.com/<owner>/<repo>/pull/<n>``.
# The number capture is used by :func:`parse_pr_number_from_gh_output`.
# We deliberately match the whole shape rather than just ``/pull/(\d+)``
# because the regex is also used against tool_result blobs that may
# contain other slashes; anchoring on ``https://github.com/`` scopes
# to a real GitHub PR link.
PR_URL_RE = re.compile(r'https://github\.com/[^\s/]+/[^\s/]+/pull/(\d+)')

# Match a ``gh pr create`` invocation as a top-level command — either
# at the start of the string or after a shell operator that begins a
# new command context (``;``, ``&``, ``|``, ``(``, or bare
# whitespace after a chain). Deliberately DOES NOT match the same
# text nested inside a quoted argument (``git commit -m "gh pr
# create in message"``) because the char immediately preceding
# ``gh`` would be a quote, which isn't in the allowed leading-char
# class.
#
# Not a full shell parser — good enough for the SDK loop's needs
# because the downstream URL parse gates any residual false-positives.
_GH_PR_CREATE_RE = re.compile(r'(?:^|[\s;&|(])gh\s+pr\s+create(?:\s|$)')


def parse_pr_number_from_gh_output(text: str) -> int | None:
    """Parse the PR number from a ``gh pr create`` stdout blob.

    Returns the integer PR number, or ``None`` when the text does not
    contain a recognisable GitHub PR URL. Whitespace + trailing
    newlines are tolerated (``gh pr create`` writes the URL followed
    by ``\\n``, so a bare ``strip()`` isn't sufficient — the regex
    handles both cases uniformly).

    This helper is the SHARED source-of-truth for URL parsing. Two
    call-sites currently import it:

      * :mod:`gate.tools.pr_back` — parses the URL returned by the
        MCP admin's ``gh pr create`` subprocess.

      * :mod:`gate.agent.initiative` — parses the URL out of the
        ``ToolResultBlock`` following a Bash ``gh pr create`` call
        during the SDK loop.
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


def is_gh_pr_create_command(command: str) -> bool:
    """Classify a Bash tool invocation as a ``gh pr create`` call.

    Matches ``gh pr create`` as a top-level command via the
    :data:`_GH_PR_CREATE_RE` regex — either at string-start or
    following a shell operator that begins a new command context
    (``;``, ``&``, ``|``, ``(``, or whitespace). Deliberately DOES
    NOT match the same text nested inside a quoted argument (``git
    commit -m "gh pr create in message"``) because the character
    immediately before ``gh`` in that case is a quote, which the
    regex excludes.

    Not a full shell parser — good enough for the SDK loop's needs
    because the downstream URL parse in
    :func:`parse_pr_number_from_gh_output` is a second gate: any
    residual false-positive that finds no PR URL in the tool_result
    still yields None and no publish happens.

    Matches (all TRUE):

      * ``gh pr create --title X --body Y``
      * ``cd /workspace/repo && gh pr create --title X`` — ``&`` in
        the leading-char class.
      * ``gh pr create --title X | tee /tmp/pr.log`` — pipe after.
      * ``gh pr create; echo done`` — semicolon after.

    Non-matches (all FALSE):

      * ``gh pr view 42`` — different subcommand.
      * ``gh pr comment 42 --body /hold`` — different subcommand.
      * ``gh pr list --head agent/foo`` — different subcommand.
      * ``git commit -m "gh pr create in message"`` — text nested
        inside a quoted commit message; ``"`` is not in the
        leading-char class.
    """
    if not command:
        return False
    return bool(_GH_PR_CREATE_RE.search(command))
