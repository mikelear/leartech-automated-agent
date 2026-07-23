"""Self-retrospective phase — runs after 'Ready for client review' sticky.

Asks one targeted LLM call: 'what should I have caught locally before
pushing?' Output filed as a GitHub Issue on the originating repo with
the label ``self-retrospective``, containing structured findings each
with a root cause + proposed enhancement.

Triggered post-success from the job_reconciler when a run reaches
terminal=complete (Phase F: every run is a K8s Job, so the reconciler
is the only completion signal).
Non-blocking: any failure here MUST NOT affect the agent's main flow.
The PR is already merged-eligible; the retrospective is enrichment.

## Why this module exists

Real incident 2026-05-28: PR #27 added Azure OpenAI 4th reviewer to
ai-review-worker. The agent did NOT catch that the consuming Tekton
tasks (leartech-pipeline-catalog/tasks/ai-review/*.yaml) needed their
env block updated to surface AZURE_OPENAI_*. The gap surfaced only
when the manual audit ran with only 2 reviewers (PRs #46/#1 today
were the fix). The agent had no cross-repo-consistency check at
PR-time to catch this.

This module gives the agent a structured way to recognise "I should
have caught this" after the fact and file the recognition as
actionable feedback. Over time the findings inform new calibration
lessons / criteria / tekton steps, closing the gap proactively.

## Cost model

Each retrospect call is ~1 LLM round trip on Opus (~2000 output
tokens ≈ ~$0.25). For 100 PRs/day that's $25/day — acceptable for
the leverage but not zero, so we skip trivial PRs (<10 lines) and
gate the whole thing behind ``LEARTECH_AGENT_SELF_RETROSPECT`` so a
cluster can disable it via chart values if rollout reveals issues.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, cast

logger = logging.getLogger(__name__)


# Default model for the retrospect call. Mirrors the main agent default
# (claude-opus-4-7) so the verdict is rendered at the same quality bar
# as the work being retrospected. Override via env if cluster wants
# cheaper or faster.
DEFAULT_RETROSPECT_MODEL = os.environ.get('LEARTECH_RETROSPECT_MODEL', 'claude-opus-4-7')

# Skip retrospect on trivial PRs — not worth $0.25 to introspect a
# one-line fix. Counted as raw lines in the unified diff (added + removed),
# excluding diff headers.
MIN_DIFF_LINES_FOR_RETROSPECT = 10

# Cap the diff we send to the LLM. Larger PRs get truncated with a marker.
# 80k chars ≈ 20k tokens ≈ ~$0.05 input cost; combined with the response
# cap this keeps the worst-case bill bounded.
MAX_DIFF_CHARS = 80_000


_VALID_FORMS = {'lesson', 'criterion', 'tekton-step', 'pre-push-check'}
_VALID_PRIORITIES = {'high', 'medium', 'low'}


@dataclass(frozen=True)
class Finding:
    """One actionable observation from the retrospective LLM call."""

    title: str
    root_cause: str
    proposed_fix: str
    suggested_form: str  # lesson | criterion | tekton-step | pre-push-check
    priority: str  # high | medium | low


def _count_diff_lines(diff: str) -> int:
    """Count added+removed lines in a unified diff, ignoring headers and hunk markers."""
    count = 0
    for line in diff.splitlines():
        if not line:
            continue
        first = line[0]
        # Skip diff metadata: `+++ b/...`, `--- a/...`, `@@ ... @@`, `diff --git`, `index ...`
        if line.startswith(('+++', '---', '@@', 'diff ', 'index ', 'new file', 'deleted file', 'Binary', 'similarity')):
            continue
        if first in ('+', '-'):
            count += 1
    return count


def _build_retrospect_prompt(pr_diff: str, ai_review_verdict: str | None, gate_state: str) -> str:
    """Render the retrospect user prompt. The structured-output schema is enforced
    via explicit instructions; we parse the JSON object out of the model's response.
    """
    diff_section = pr_diff
    if len(diff_section) > MAX_DIFF_CHARS:
        diff_section = (
            diff_section[:MAX_DIFF_CHARS]
            + f'\n\n[diff truncated at {MAX_DIFF_CHARS} chars — original was {len(pr_diff)} chars]'
        )

    return f"""You are reviewing your own PR work post-hand-off. Below is the PR diff,
the AI code review verdict, and the final gate state.

Your job: identify things that you (the agent) should have caught
LOCALLY before pushing — i.e. checks that don't require waiting for
PR-time gates or human review to surface.

Examples of things to identify:
- Cross-file consistency gaps (e.g. added a new env var consumer but
  didn't update the producer manifest)
- Tekton task / cluster config out-of-sync with code changes
- Missing pre-push lint / format that would have caught simple issues
- Cases where the AI review found genuine bugs that a faster local
  check could have caught

Output ONLY a JSON object with this schema (no markdown fences, no
prose before or after):

{{
  "findings": [
    {{
      "title": "short summary",
      "root_cause": "why it slipped past local checks",
      "proposed_fix": "concrete enhancement",
      "suggested_form": "lesson | criterion | tekton-step | pre-push-check",
      "priority": "high | medium | low"
    }}
  ]
}}

If you genuinely have no findings, return {{"findings": []}}. Do not
invent findings to fill space — false positives are worse than empty.

--- PR DIFF ---
{diff_section}

--- AI REVIEW VERDICT ---
{ai_review_verdict or '(none — AI review did not run or returned no body)'}

--- FINAL GATE STATE ---
{gate_state}
"""


_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)


def _parse_findings(response_text: str) -> list[Finding]:
    """Extract the JSON object from the LLM response and parse into Findings.

    Tolerant of:
      - leading/trailing prose around the JSON
      - markdown ``` ```json ``` fences
      - the model returning extra keys per finding
    """
    text = response_text.strip()
    # Strip markdown fences if present
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```\s*$', '', text)

    # Try direct parse first; fall back to greedy braces match
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise
        payload = json.loads(match.group(0))

    raw_findings = payload.get('findings', []) if isinstance(payload, dict) else []
    findings: list[Finding] = []
    for raw in raw_findings:
        if not isinstance(raw, dict):
            continue
        try:
            form = str(raw['suggested_form']).strip()
            priority = str(raw['priority']).strip()
            if form not in _VALID_FORMS or priority not in _VALID_PRIORITIES:
                logger.debug('self_retrospect dropping finding with invalid form/priority: %r', raw)
                continue
            findings.append(
                Finding(
                    title=str(raw['title']).strip(),
                    root_cause=str(raw['root_cause']).strip(),
                    proposed_fix=str(raw['proposed_fix']).strip(),
                    suggested_form=form,
                    priority=priority,
                )
            )
        except KeyError as exc:
            logger.debug('self_retrospect dropping finding missing key %s: %r', exc, raw)
            continue
    return findings


async def retrospect_after_ready(
    *,
    pr_repo: str,
    pr_number: int,
    pr_diff: str,
    ai_review_verdict: str | None,
    final_gate_state: str,
    model: str | None = None,
) -> list[Finding]:
    """Make one LLM call. Parse structured findings. Filter low-priority + empty.

    Returns the filtered findings; the caller files the Issue.
    Behaviour-preserving: if LLM call fails for any reason, returns [].

    Trivial PRs (under MIN_DIFF_LINES_FOR_RETROSPECT changed lines) are
    skipped — not worth the LLM cost to introspect a one-line typo fix.
    """
    diff_lines = _count_diff_lines(pr_diff)
    if diff_lines < MIN_DIFF_LINES_FOR_RETROSPECT:
        logger.info(
            'self_retrospect skipped for %s#%d (PR too small: %d lines)',
            pr_repo,
            pr_number,
            diff_lines,
        )
        return []

    prompt = _build_retrospect_prompt(pr_diff, ai_review_verdict, final_gate_state)
    chosen_model = model or DEFAULT_RETROSPECT_MODEL

    try:
        # LLM call goes through the provider seam (gate.llm) — the single
        # anthropic import site. to_thread keeps the sync SDK call off the loop.
        from gate import llm

        resp = await asyncio.to_thread(
            llm.complete,
            model=chosen_model,
            max_tokens=2000,
            messages=cast(Any, [{'role': 'user', 'content': prompt}]),
        )
    except Exception as exc:  # noqa: BLE001 — non-blocking: log + bail
        logger.warning('self_retrospect LLM call failed: %s', exc)
        return []

    try:
        first_block = resp.content[0]
        # The Messages API returns content blocks; we expect a single text block.
        text = getattr(first_block, 'text', None)
        if text is None:
            logger.warning('self_retrospect: response did not include a text block')
            return []
        findings = _parse_findings(text)
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        logger.warning('self_retrospect parse failed: %s', exc)
        return []

    # Filter low-priority — avoid noise on every PR.
    filtered = [f for f in findings if f.priority in ('high', 'medium')]
    logger.info(
        'self_retrospect for %s#%d: %d findings raw, %d after filter',
        pr_repo,
        pr_number,
        len(findings),
        len(filtered),
    )
    return filtered


def _render_issue_body(pr_number: int, findings: list[Finding]) -> str:
    """Markdown-format the findings as an Issue body."""
    lines: list[str] = [
        f'Self-retrospective for PR #{pr_number}.',
        '',
        'These findings were identified post-hand-off by the agent reviewing its '
        'own diff against the final gate state. Each suggests a concrete '
        'enhancement that would have caught the issue locally before push.',
        '',
        '> See the `self-retrospect-honesty` calibration lesson — findings here '
        '> should be acted on (lesson / criterion / tekton-step / pre-push-check) '
        '> or rejected with a reason.',
        '',
        '---',
        '',
    ]
    for idx, f in enumerate(findings, start=1):
        lines.extend(
            [
                f'## {idx}. {f.title}',
                '',
                f'**Priority**: {f.priority}  ·  **Suggested form**: `{f.suggested_form}`',
                '',
                '**Root cause**:',
                '',
                f.root_cause,
                '',
                '**Proposed fix**:',
                '',
                f.proposed_fix,
                '',
                '---',
                '',
            ]
        )
    return '\n'.join(lines)


async def file_issue_with_findings(
    *,
    pr_repo: str,
    pr_number: int,
    findings: list[Finding],
) -> str | None:
    """Open a GitHub Issue with the structured findings. Return issue URL or None.

    Uses ``gh issue create`` which is already authed in the pod via
    GITHUB_TOKEN. Best-effort: returns None on any failure. If the
    ``self-retrospective`` label or ``candidate/*`` labels don't exist
    yet on the target repo, retries once without labels so the
    enrichment isn't lost.
    """
    if not findings:
        return None  # nothing to file

    title = f'Self-retrospective: PR #{pr_number} — {len(findings)} preventable finding(s)'
    body = _render_issue_body(pr_number, findings)
    labels = ['self-retrospective', *sorted({f'candidate/{f.suggested_form}' for f in findings})]

    label_args: list[str] = []
    for lbl in labels:
        label_args.extend(['--label', lbl])

    base_cmd = [
        'gh',
        'issue',
        'create',
        '--repo',
        pr_repo,
        '--title',
        title,
        '--body',
        body,
    ]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [*base_cmd, *label_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        # Most likely cause of failure: label doesn't exist on the repo yet.
        # Retry once without labels so the Issue still lands.
        logger.warning(
            'self_retrospect: gh issue create with labels failed (exit %d): %s — retrying without labels',
            result.returncode,
            result.stderr.strip(),
        )
        retry = await asyncio.to_thread(
            subprocess.run,
            base_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if retry.returncode == 0:
            return retry.stdout.strip() or None
        logger.warning(
            'self_retrospect: gh issue create retry also failed (exit %d): %s',
            retry.returncode,
            retry.stderr.strip(),
        )
        return None
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning('self_retrospect: gh issue create errored: %s', exc)
        return None


async def fetch_pr_diff(pr_repo: str, pr_number: int) -> str:
    """Best-effort: return the PR's unified diff via `gh pr diff`. Empty string on failure."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ['gh', 'pr', 'diff', str(pr_number), '--repo', pr_repo],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning('self_retrospect: gh pr diff failed (exit %d): %s', result.returncode, result.stderr.strip())
            return ''
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning('self_retrospect: gh pr diff errored: %s', exc)
        return ''


async def fetch_ai_review_verdict(pr_repo: str, pr_number: int) -> str | None:
    """Best-effort: return the most recent AI-review sticky body, or None.

    Heuristic: look at PR comments + reviews, return the first body that
    contains the AI-review marker. Returns None on any failure or if no
    AI review sticky was posted.
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                'gh',
                'pr',
                'view',
                str(pr_number),
                '--repo',
                pr_repo,
                '--json',
                'comments,reviews',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or '{}')
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        logger.warning('self_retrospect: ai review fetch errored: %s', exc)
        return None

    candidates: list[str] = []
    for c in data.get('comments') or []:
        body = c.get('body') or ''
        if body and ('ai-review' in body.lower() or 'ai code review' in body.lower()):
            candidates.append(body)
    for r in data.get('reviews') or []:
        body = r.get('body') or ''
        if body and ('ai-review' in body.lower() or 'ai code review' in body.lower()):
            candidates.append(body)
    return candidates[-1] if candidates else None


async def fetch_gate_state(pr_repo: str, pr_number: int) -> str:
    """Best-effort: return a compact text summary of the PR's check states.

    Uses ``gh pr checks`` which surfaces every required check across
    clusters. Empty string on failure — the LLM call will still run with
    just the diff + AI review.
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ['gh', 'pr', 'checks', str(pr_number), '--repo', pr_repo],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        # `gh pr checks` exits non-zero if any check is failing — we still want
        # the body in that case, so don't gate on returncode.
        return result.stdout or ''
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning('self_retrospect: gh pr checks errored: %s', exc)
        return ''
