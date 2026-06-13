"""AI code review verdicts parsed from PR sticky comments.

The catalog's `tasks/ai-review/pullrequest.yaml` posts a comment per cluster:

    ## :white_check_mark: AI Code Review: **95/100 — Excellent** `[gcp]`

    **Scores:** 95 | 95 (avg: 95) | **3 reviewers**

    > All reviewers passed. This PR is eligible for auto-merge.

The leading emoji is the canonical verdict — `:white_check_mark:` (pass), `:warning:`
(needs work), `:x:` (fail). We parse it plus the numeric score and the auto-merge line.

Warning/fail verdicts also include a per-finding ``### Issues Found`` block (one
bullet per reviewer objection) that the agent's iteration loop wants for
structured fix-it context. We parse those into :class:`AIReviewFinding` records
so the watcher (see :mod:`gate.watcher.ai_review_iteration`) can decide whether
to re-spawn the agent with structured red findings as feedback, or escalate to a
human when the reviewers' aggregate signal is too low to trust.

Note: ai-review is informational — it doesn't post a GitHub status check. So criteria
that gate on AI review must read these comments, not the pipelineRun status.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

# `## :white_check_mark: AI Code Review: **95/100 — Excellent** `[gcp]``
_HEADER_RE = re.compile(
    r'##\s+:(?P<emoji>white_check_mark|warning|x):\s+AI Code Review:\s+\*\*(?P<score>\d+)/100\s+—\s+(?P<verdict>[^*]+?)\*\*\s+`\[(?P<cluster>gcp|az)\]`',
)
_AUTO_MERGE_LINE = 'eligible for auto-merge'

# Finding bullets in the ``### Issues Found`` section:
#
#   - :red_circle: [claude] `path/to/file.go:42` Some objection text
#   - :yellow_circle: [deepseek] `Dockerfile:27` Hardcoded secret
#   - :blue_circle: [claude] `OWNERS:5` Unnecessary trailing whitespace
#
# Captured groups: severity (red/yellow/blue), reviewer, location, fix_hint.
# Whitespace is permissive so a future reviewer rendering tweak (extra spaces)
# doesn't break parsing. Anchored to the leading dash + space to avoid matching
# the same shape if it appears inside a suggestion or quote.
_FINDING_RE = re.compile(
    r'^\s*-\s+:(?P<severity>red_circle|yellow_circle|blue_circle):\s+'
    r'\[(?P<reviewer>[^\]]+)\]\s+'
    r'`(?P<location>[^`]+)`\s+'
    r'(?P<fix_hint>.+?)\s*$',
    re.MULTILINE,
)

# Map the verdict emoji's circle-suffix to the short severity token we expose.
_SEVERITY_MAP = {
    'red_circle': 'red',
    'yellow_circle': 'yellow',
    'blue_circle': 'blue',
}


@dataclass(frozen=True)
class AIReviewFinding:
    """One reviewer objection captured from a verdict comment's Issues Found block.

    The fields mirror the bullet shape so the agent's prompt context can cite
    the location + fix_hint verbatim — no extra interpretation needed before
    surfacing the finding to the next iteration.
    """

    severity: str  # 'red' | 'yellow' | 'blue'
    reviewer: str  # e.g. 'claude' | 'deepseek' | 'ollama'
    location: str  # e.g. 'cmd/server/main.go:67' — file:line, sometimes just a file
    fix_hint: str  # the descriptive text after the location, as written

    @property
    def is_red(self) -> bool:
        return self.severity == 'red'

    def to_dict(self) -> dict[str, str]:
        """JSON-serialisable shape for the feedback_payloads contract."""
        return {
            'severity': self.severity,
            'reviewer': self.reviewer,
            'location': self.location,
            'fix_hint': self.fix_hint,
        }


@dataclass(frozen=True)
class AIReviewVerdict:
    """A single ai-review sticky comment's parsed view.

    Carries the header (cluster + emoji + score + verdict word + auto-merge
    eligibility line) plus the structured Issues Found bullets when present.
    ``findings`` is empty for fully-passing verdicts (Excellent / no objections);
    callers should test :attr:`red_findings` rather than truthiness on the
    full set when deciding whether to iterate.
    """

    cluster: str  # 'gcp' | 'az'
    emoji: str  # 'white_check_mark' | 'warning' | 'x'
    score: int  # 0-100
    verdict: str  # 'Excellent', 'Needs Work', etc.
    auto_merge_eligible: bool
    findings: tuple[AIReviewFinding, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.emoji == 'white_check_mark'

    @property
    def blocking(self) -> bool:
        """Hard fail — the AI review explicitly flagged a problem."""
        return self.emoji == 'x'

    @property
    def red_findings(self) -> tuple[AIReviewFinding, ...]:
        """Subset of findings flagged ``:red_circle:`` — the agent must address these."""
        return tuple(f for f in self.findings if f.is_red)


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def parse_ai_review_findings(body: str) -> list[AIReviewFinding]:
    """Extract the bullet-list of objections from an ai-review verdict body.

    Scopes parsing to the ``### Issues Found`` section if present — bullets in
    other sections (Suggestions, feedback panel) use the same emoji shapes and
    would otherwise match the regex. When the Issues Found section is missing
    (fully-passing verdicts) returns an empty list.

    The function is permissive about the closing boundary: the next ``###``
    header, the closing ``</details>`` tag, or end-of-string all terminate the
    section. A future renderer change that inserts a sibling section ahead of
    Suggestions will continue to work.
    """
    issues_marker = '### Issues Found'
    start = body.find(issues_marker)
    if start < 0:
        return []
    # Trim everything before the Issues Found header so the regex's anchored
    # bullets can't latch onto unrelated content earlier in the comment.
    region = body[start + len(issues_marker) :]
    # End the region at the next H3 header or details fold so we don't slurp
    # in Suggestions / feedback panels.
    end_markers = ('\n### ', '\n<details', '\n---\n')
    end = len(region)
    for marker in end_markers:
        idx = region.find(marker)
        if idx >= 0 and idx < end:
            end = idx
    region = region[:end]
    out: list[AIReviewFinding] = []
    for match in _FINDING_RE.finditer(region):
        severity_token = match.group('severity')
        severity = _SEVERITY_MAP.get(severity_token)
        if severity is None:
            continue
        out.append(
            AIReviewFinding(
                severity=severity,
                reviewer=match.group('reviewer').strip(),
                location=match.group('location').strip(),
                fix_hint=match.group('fix_hint').strip(),
            )
        )
    return out


def parse_ai_review_comment(body: str) -> AIReviewVerdict | None:
    """Extract a verdict from a single comment body. Returns None if not an AI review comment."""
    if 'AI Code Review' not in body:
        return None
    match = _HEADER_RE.search(body)
    if not match:
        return None
    return AIReviewVerdict(
        cluster=match.group('cluster'),
        emoji=match.group('emoji'),
        score=int(match.group('score')),
        verdict=match.group('verdict').strip(),
        auto_merge_eligible=_AUTO_MERGE_LINE in body,
        findings=tuple(parse_ai_review_findings(body)),
    )


def read_ai_review_verdicts(repo: str, pr_number: int) -> list[AIReviewVerdict]:
    """Returns one verdict per AI review comment found on the PR. Empty list if none posted yet.

    When a cluster has multiple AI review comments (re-runs), keeps only the most recent.
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    raw = _gh(['pr', 'view', str(pr_number), '-R', qualified, '--json', 'comments'])
    comments = json.loads(raw).get('comments', [])

    verdicts: list[AIReviewVerdict] = []
    seen_clusters: set[str] = set()
    # Walk newest-first so we keep only the most recent verdict per cluster (re-runs supersede).
    for c in reversed(comments):
        verdict = parse_ai_review_comment(c.get('body', ''))
        if verdict is None or verdict.cluster in seen_clusters:
            continue
        seen_clusters.add(verdict.cluster)
        verdicts.append(verdict)
    return verdicts
