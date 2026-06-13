"""AI-review red-finding → iteration-loop dispatch.

v6p0.6 step 4 of 4. The watcher already parses ai-review verdict comments
(see :mod:`gate.tools.ai_review`) and surfaces warning / blocking emoji
verdicts back through the existing ``ai_review_finding`` softer payload
shape. It does NOT, however, treat structured **red findings** the same
way the end2end / security / lint paths treat structured failures: a
verdict landing with red bullets sits on the PR until a human operator
types ``/retest`` or ``/test ai-code-review`` to re-fire. The agent never
sees the structured fix-it context.

This module closes the gap. It is the pure decision-shape sibling of
:mod:`gate.watcher.iteration_loop`: given a parsed
:class:`~gate.tools.ai_review.AIReviewVerdict` (or a list of them — one
per cluster) it answers the single question the PR watcher needs to ask:

    "Given this ai-review verdict, should the orchestrator (a) re-spawn
     the agent with the structured red findings injected, (b) escalate
     to a human because the reviewers' aggregate confidence is too low
     to trust the verdict, or (c) do nothing because the verdict is
     all-green / all-yellow / already-handled?"

The decision rules are:

1. **NOOP** — verdict has no red findings (all green or yellow-only); the
   verdict is already handled (idempotency key match); or the score is
   passing AND no red findings exist (the auto-merge path will pick it up).
2. **ESCALATE_LOW_CONFIDENCE** — red findings are present AND the
   aggregate score is below the 86 cutoff. The reviewers themselves
   don't agree on the verdict; the existing
   ``feedback_both_ai_review_fail_means_stop`` Class A guidance says the
   agent's substantive disagreement is real here. We stop and ask a
   human.
3. **RESPAWN** — red findings are present AND the aggregate score is
   ≥86. The reviewers DO trust their own verdict, so the findings are
   actionable; re-spawn the agent with them in ``feedback_payloads``.

The ``ai_review_failure`` payload kind this module emits is the structured
counterpart of the older ``ai_review_finding`` shape: same envelope
(``kind:``, cluster, verdict, score) but with the per-bullet structure
preserved so the agent can cite the location + reviewer + fix_hint
verbatim when iterating.

The 86 cutoff matches the existing ``test_ai_review_passing_on_every_cluster``
threshold — verdicts at or above 86/100 with a ``:warning:`` emoji are
"trusted reviewers flagged something specific"; below 86 is
"reviewers themselves don't trust the verdict".
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from gate.tools.ai_review import AIReviewFinding, AIReviewVerdict

logger = logging.getLogger(__name__)


# ─── Decision enum ───────────────────────────────────────────────────────────


class AIReviewDecision(StrEnum):
    """The four terminal verdicts of :func:`decide_ai_review_action`."""

    #: Spawn a new agent run on the same PR/branch with the structured red
    #: findings injected as feedback_payloads. The agent iterates.
    RESPAWN = 'respawn'

    #: Red findings present AND aggregate score < :data:`SCORE_CONFIDENCE_THRESHOLD`.
    #: Per the ``feedback_both_ai_review_fail_means_stop`` Class A pattern,
    #: the reviewers themselves don't trust the verdict; further automated
    #: iteration is unlikely to converge. Hand off to a human.
    ESCALATE_LOW_CONFIDENCE = 'escalate_low_confidence'

    #: Nothing actionable — no red findings, or all unhandled red verdicts
    #: are already in :attr:`AIReviewIterationContext.already_handled_keys`.
    NOOP = 'noop'


# ─── Constants ───────────────────────────────────────────────────────────────


#: Aggregate score at or above which we trust a verdict's red findings as
#: actionable agent feedback. Below this we escalate per
#: ``feedback_both_ai_review_fail_means_stop`` Class A guidance.
SCORE_CONFIDENCE_THRESHOLD: Final = 86


# ─── Context ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AIReviewIterationContext:
    """Inputs to :func:`decide_ai_review_action`.

    Construct from the PR watcher's already-fetched verdict comments (one
    per cluster) plus the orchestrator's handled-key set.
    """

    repo: str  # ``<owner>/<name>`` — used only for diagnostics / templating
    pr_number: int

    #: Verdicts the watcher fetched for this PR. Typically 1-2 (one per
    #: cluster). The decision logic considers them together: if ANY
    #: cluster's verdict has red findings the agent should iterate;
    #: if ANY cluster's verdict is below the confidence threshold the
    #: agent escalates.
    verdicts: tuple[AIReviewVerdict, ...] = field(default_factory=tuple)

    #: Idempotency-key set. The orchestrator records every key it has
    #: already acted on (a sticky-comment marker, a Job label, a DB
    #: column). Any verdict whose :func:`verdict_idempotency_key` is in
    #: this set is dropped from the decision input — preventing the
    #: orchestrator from re-spawning on the same verdict twice.
    already_handled_keys: frozenset[str] = field(default_factory=frozenset)


# ─── Decision ────────────────────────────────────────────────────────────────


def decide_ai_review_action(ctx: AIReviewIterationContext) -> AIReviewDecision:
    """Return what the orchestrator should do given this set of verdicts.

    Decision precedence (top wins):

    1. **NOOP** — no verdicts; OR no verdict has red findings; OR every
       verdict-with-red-findings is already in ``already_handled_keys``.
    2. **ESCALATE_LOW_CONFIDENCE** — at least one unhandled verdict has
       red findings AND its score is below :data:`SCORE_CONFIDENCE_THRESHOLD`.
       Class A: stop, escalate.
    3. **RESPAWN** — at least one unhandled verdict has red findings AND
       all such verdicts have score ≥ :data:`SCORE_CONFIDENCE_THRESHOLD`.
       Re-spawn the agent with the structured findings.

    Mixed clusters: if one cluster's verdict is high-score-with-red and
    another is low-score-with-red, escalate. The low-score signal
    dominates — if either cluster's reviewers don't trust their own
    verdict, the agent shouldn't iterate on it either.
    """
    if not ctx.verdicts:
        return AIReviewDecision.NOOP

    unhandled_with_red = [
        v for v in ctx.verdicts if v.red_findings and verdict_idempotency_key(v) not in ctx.already_handled_keys
    ]

    if not unhandled_with_red:
        return AIReviewDecision.NOOP

    # Any low-score unhandled verdict with red findings → escalate.
    # The reviewers themselves disagree → don't iterate; ask a human.
    if any(v.score < SCORE_CONFIDENCE_THRESHOLD for v in unhandled_with_red):
        return AIReviewDecision.ESCALATE_LOW_CONFIDENCE

    return AIReviewDecision.RESPAWN


# ─── Payload construction ────────────────────────────────────────────────────


def build_ai_review_failure_payload(verdict: AIReviewVerdict) -> dict[str, Any]:
    """Build the structured ``ai_review_failure`` payload for one verdict.

    The shape mirrors the v6p0.5 ``end2end_failure`` envelope: a discriminator
    key (``kind:``), a free-text summary, and a structured list of items the
    agent must address. Each item carries severity + reviewer + location +
    fix_hint so the next iteration's prompt can cite them verbatim — no
    re-interpretation needed before surfacing the red findings to Claude.

    ``red_findings_only=True`` by default; we drop yellow/blue noise from the
    payload because the iteration loop is keyed on red. The agent's
    introspection of the full verdict is still available via the source
    comment if it wants the wider context.
    """
    red = verdict.red_findings
    summary_parts = [
        f'AI code review on cluster `{verdict.cluster}` returned',
        f'{verdict.emoji} {verdict.verdict} ({verdict.score}/100)',
        f'with {len(red)} red finding(s) the agent must address.',
    ]
    return {
        'kind': 'ai_review_failure',
        'cluster': verdict.cluster,
        'verdict': verdict.verdict,
        'score': verdict.score,
        'emoji': verdict.emoji,
        'summary': ' '.join(summary_parts),
        'red_findings': [f.to_dict() for f in red],
        # Surface all findings (yellow/blue too) so the agent can resolve
        # closely-related context if a red finding's fix would naturally
        # touch the same file as a yellow note.
        'all_findings': [f.to_dict() for f in verdict.findings],
    }


def build_ai_review_failure_payloads(
    verdicts: tuple[AIReviewVerdict, ...] | list[AIReviewVerdict],
) -> list[dict[str, Any]]:
    """Build payloads for every verdict that has red findings.

    Verdicts without red findings (all-green / yellow-only) are skipped —
    the iteration loop only spawns on actionable red signal. The
    orchestrator never sees the "no red findings" verdicts via this path;
    they fall through to the existing :data:`ai_review_finding` softer
    payload kind for context when an unrelated gate triggered the respawn.
    """
    return [build_ai_review_failure_payload(v) for v in verdicts if v.red_findings]


# ─── Prompt rendering ────────────────────────────────────────────────────────


def format_ai_review_failure_payload(payload: dict[str, Any]) -> list[str]:
    """Render one ``ai_review_failure`` payload as prompt-ready markdown lines.

    Shape:

        ### Previous attempt: AI code review (gcp) — Needs Work (88/100)
        2 red finding(s) the agent must address.

        **Red findings**:
        - [claude] `cmd/server/main.go:67` Description...
        - [deepseek] `Dockerfile:27` Description...

    Mirrored to be visually consistent with the ``end2end_failure`` block
    rendering so reviewers reading the agent's prompt context can scan all
    feedback blocks the same way.
    """
    lines: list[str] = []
    cluster = payload.get('cluster', '?')
    verdict = payload.get('verdict', '?')
    score = payload.get('score', '?')
    lines.append(f'### Previous attempt: AI code review ({cluster}) — {verdict} ({score}/100)')
    if payload.get('summary'):
        lines.append(payload['summary'])
    red = payload.get('red_findings') or []
    if red:
        lines.append('')
        lines.append('**Red findings (MUST address)**:')
        for f in red:
            reviewer = f.get('reviewer', '?')
            location = f.get('location', '?')
            hint = f.get('fix_hint', '')
            lines.append(f'- [{reviewer}] `{location}` — {hint}')
    other = [f for f in (payload.get('all_findings') or []) if f.get('severity') in {'yellow', 'blue'}]
    if other:
        lines.append('')
        lines.append('**Additional context (yellow/blue notes)**:')
        for f in other:
            sev = f.get('severity', '?')
            reviewer = f.get('reviewer', '?')
            location = f.get('location', '?')
            hint = f.get('fix_hint', '')
            lines.append(f'- :{sev}: [{reviewer}] `{location}` — {hint}')
    return lines


# ─── Idempotency ─────────────────────────────────────────────────────────────


def verdict_idempotency_key(verdict: AIReviewVerdict) -> str:
    """Return a stable string identifying this specific verdict observation.

    Two verdicts produce the same key when they describe the same set of
    findings on the same cluster at the same score. A re-run that
    *supersedes* the previous verdict (different findings, or same
    findings but a different score) produces a fresh key — the watcher
    will then re-evaluate.

    Inputs incorporated into the hash:

    - cluster
    - emoji + score + verdict word (the header signal)
    - per-finding tuple (severity, reviewer, location, fix_hint), sorted

    The fix_hint is included because reviewers occasionally re-issue with
    a clearer message at the same severity / location — the agent should
    see the updated context.
    """
    # Sort findings to make the key invariant of reviewer-output ordering.
    findings_canon = sorted(
        (_finding_canon(f) for f in verdict.findings),
    )
    parts = [
        verdict.cluster,
        verdict.emoji,
        str(verdict.score),
        verdict.verdict,
        '|'.join(findings_canon),
    ]
    raw = '||'.join(parts).encode('utf-8')
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]


def _finding_canon(finding: AIReviewFinding) -> str:
    """Stable canonical string for one finding, used inside the verdict key."""
    return ':'.join(
        [
            finding.severity,
            finding.reviewer,
            finding.location,
            finding.fix_hint,
        ]
    )


__all__ = [
    'AIReviewDecision',
    'AIReviewIterationContext',
    'SCORE_CONFIDENCE_THRESHOLD',
    'build_ai_review_failure_payload',
    'build_ai_review_failure_payloads',
    'decide_ai_review_action',
    'format_ai_review_failure_payload',
    'verdict_idempotency_key',
]
