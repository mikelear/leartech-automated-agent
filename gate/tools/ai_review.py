"""AI code review verdicts parsed from PR sticky comments.

The catalog's `tasks/ai-review/pullrequest.yaml` posts a comment per cluster:

    ## :white_check_mark: AI Code Review: **95/100 — Excellent** `[gcp]`

    **Scores:** 95 | 95 (avg: 95) | **3 reviewers**

    > All reviewers passed. This PR is eligible for auto-merge.

The leading emoji is the canonical verdict — `:white_check_mark:` (pass), `:warning:`
(needs work), `:x:` (fail). We parse it plus the numeric score and the auto-merge line.

Note: ai-review is informational — it doesn't post a GitHub status check. So criteria
that gate on AI review must read these comments, not the pipelineRun status.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

# `## :white_check_mark: AI Code Review: **95/100 — Excellent** `[gcp]``
_HEADER_RE = re.compile(
    r'##\s+:(?P<emoji>white_check_mark|warning|x):\s+AI Code Review:\s+\*\*(?P<score>\d+)/100\s+—\s+(?P<verdict>[^*]+?)\*\*\s+`\[(?P<cluster>gcp|az)\]`',
)
_AUTO_MERGE_LINE = 'eligible for auto-merge'


@dataclass(frozen=True)
class AIReviewVerdict:
    cluster: str  # 'gcp' | 'az'
    emoji: str  # 'white_check_mark' | 'warning' | 'x'
    score: int  # 0-100
    verdict: str  # 'Excellent', 'Needs Work', etc.
    auto_merge_eligible: bool

    @property
    def passed(self) -> bool:
        return self.emoji == 'white_check_mark'

    @property
    def blocking(self) -> bool:
        """Hard fail — the AI review explicitly flagged a problem."""
        return self.emoji == 'x'


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


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
