"""Iteration-loop decision logic — re-spawn the agent on end2end failures.

The PR watcher (step 3 of the v6p0.5 plan; not yet wired) consumes the
:class:`gate.tools.end2end_gate.End2EndFailure` payloads produced by step 1
and asks this module the single question:

    "Given the current iteration count + the failure(s) we just observed,
     should the orchestrator (a) re-spawn the agent with the failure
     context injected, (b) post an info comment and retest, or (c)
     escalate to a human?"

This module is deliberately pure: no GitHub calls, no K8s calls, no DB
state. It accepts an :class:`IterationContext` and returns an
:class:`IterationDecision`. The orchestrator (step 3) carries out the
side-effects — post the comment, apply the label, ``spawn_initiative_job``
with feedback_payloads — using whichever surface (GitHub API, Tekton MCP,
``app.routers.initiatives``) it already has wired.

Why purity matters here
-----------------------

The same decision needs to be reachable from at least three places:

1. The cluster-deployed PR watcher (step 3).
2. The Tekton task that fires on gate finalisation (a later structural fix).
3. The unit-test harness in :mod:`tests.test_end2end_iteration_loop`.

If the decision logic dragged in ``gh`` shell-outs or K8s clients, we'd
have to fake those in every test and every alternate-surface caller. By
keeping the decision pure and the side-effects at the boundary, the same
function can drive all three with identical, deterministic behaviour.

Mirrors the existing pattern in :mod:`gate.agent.step_failure_diagnosis`
(``classify_step_failure`` returns a :class:`StepFailure` action; callers
dispatch on ``action``). That module showed the value of separating
classification from execution; we apply the same shape here.

Feedback-payload shape
----------------------

The agent's startup prompt construction (see
:func:`gate.agent.initiative.run_initiative`) reads
``initiative.feedback_payloads`` and surfaces each payload to the model
as a "previous attempt failed these checks — read details and fix" block.
Two payload kinds are supported today:

- ``end2end_failure`` — produced by this module from an ``End2EndFailure``.
  Carries the gate name, classification, summary, per-test messages, and
  (for end2end-ui) screenshot / trace URLs.
- ``ai_review_finding`` — produced by the existing ai-review path
  (warning / blocking verdicts on AI Code Review). Merges into the same
  list so the agent sees one unified "here is what failed last time"
  block regardless of which gate produced the feedback.

Both payloads are plain JSON-serialisable dicts so they survive the
round-trip through the catalog / Job-spec env injection without needing a
custom serializer.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from gate.tools.end2end_gate import End2EndFailure, End2EndTest

logger = logging.getLogger(__name__)


# ─── Decision enum ───────────────────────────────────────────────────────────


class IterationDecision(StrEnum):
    """The four terminal verdicts of :func:`decide_action`.

    Subclasses :class:`enum.StrEnum` (Python 3.11+) so the values are
    JSON-serialisable for the orchestrator's structured logging without a
    custom encoder, and string comparisons (``decision == 'respawn'``)
    work transparently.
    """

    #: Spawn a new agent run on the same PR/branch with ``feedback_payloads``
    #: populated. The agent reads the payloads at startup and iterates.
    RESPAWN = 'respawn'

    #: Preview-infra failure. Post an info comment ("preview-infra detected,
    #: retrying gate"); do NOT spawn a new agent run. The Tekton ``/test``
    #: retest path handles the actual retry.
    SKIP_INFRA = 'skip_infra'

    #: Two or more preview-infra hits in a row. Promote the skip into an
    #: explicit hand-off so the cluster operator sees it as
    #: ``requires_operator`` rather than churning forever.
    ESCALATE_REPEATED_INFRA = 'escalate_repeated_infra'

    #: The iteration count would exceed ``max_iterations`` if we re-spawned.
    #: Apply the ``do-not-merge/manual-fix`` label + summary comment;
    #: hand off to a human.
    ESCALATE_MAX_ITERATIONS = 'escalate_max_iterations'

    #: Nothing actionable to do (no failures, or all real_failure entries
    #: are already in :attr:`IterationContext.already_handled_keys`).
    NOOP = 'noop'


# ─── Inputs ──────────────────────────────────────────────────────────────────


# Max iterations default — small intentionally. The agent has its own
# in-session SDK loop already (an "iteration" here is a full re-spawn /
# new Job pod). Three full respawns is enough headroom for the canonical
# "read /health/live, fix endpoint, re-deploy, re-test" cycle; more than
# that and the failure shape is almost always something a human needs to
# look at.
DEFAULT_MAX_ITERATIONS: Final = 3

# Threshold at which "skip_infra" promotes to "escalate_repeated_infra".
# A single preview-infra hit is normal — the cluster just hadn't finished
# the deploy yet. Two in a row almost always means the preview is genuinely
# wedged (Hydra DNS, OOMKilled pod, ImagePullBackOff) and a human should
# triage rather than the agent retesting in an automated loop.
REPEATED_INFRA_THRESHOLD: Final = 2


@dataclass(frozen=True)
class PriorAttempt:
    """A single past agent run on the same PR, used by the orchestrator
    to compute iteration count + summarise attempts in the escalation
    comment.

    The orchestrator builds these from either the ``initiative_runs``
    DB table (when DSN is set) or by counting agent-authored commits /
    sticky comments on the PR. The shape is what
    :func:`manual_fix_comment_body` consumes, so we keep it stable here.
    """

    run_id: str
    started_at: str  # ISO-8601 from the DB column; we don't parse it
    status: str  # 'complete' | 'failed' | 'cancelled' | ...
    summary: str = ''  # free-text — what the agent said it did


@dataclass(frozen=True)
class IterationContext:
    """Inputs to :func:`decide_action`.

    Construct one of these from the PR state the orchestrator already
    queries (Tekton check status + DB ``initiative_runs`` rows + parsed
    sticky comments) and pass it in.
    """

    repo: str  # ``<owner>/<name>`` — used only for diagnostics + comment templating
    pr_number: int

    #: Failures produced by this gate-finalisation event. The orchestrator
    #: gathers all failed end2end / end2end-ui checks for this PR (both
    #: clusters) and builds the payloads via
    #: :func:`gate.tools.end2end_gate.fetch_end2end_failure`.
    failures: tuple[End2EndFailure, ...] = field(default_factory=tuple)

    #: How many full agent re-spawn cycles have already fired on this PR.
    #: The first event has count=0 — i.e. "no prior respawns".
    iteration_count: int = 0

    #: How many consecutive preview-infra outcomes we've already observed
    #: on this PR. Resets to 0 once a non-infra failure (or success) is
    #: seen by the orchestrator. The decision logic promotes to
    #: ``ESCALATE_REPEATED_INFRA`` when this would become
    #: :data:`REPEATED_INFRA_THRESHOLD` after the current event.
    prior_infra_count: int = 0

    #: Hard ceiling on iterations. Configurable per-initiative via
    #: ``Initiative.max_iterations``; defaults to
    #: :data:`DEFAULT_MAX_ITERATIONS` (3).
    max_iterations: int = DEFAULT_MAX_ITERATIONS

    #: Idempotency-key set. The orchestrator records every key it has
    #: already acted on (label on the spawned Job, marker comment on the
    #: PR, ``initiative_runs.feedback_idempotency_key`` column when DB
    #: is enabled). Any failure in :attr:`failures` whose
    #: :func:`failure_idempotency_key` is in this set is dropped before
    #: the decision computes — preventing the orchestrator from
    #: re-spawning on the same failure twice.
    already_handled_keys: frozenset[str] = field(default_factory=frozenset)

    #: Existing ai-review findings on this PR, surfaced so they merge into
    #: the same ``feedback_payloads`` list when we DO respawn. Optional —
    #: the orchestrator passes ``None`` when ai-review feedback wiring is
    #: not yet ready for this repo (see step 3 of v6p0.5 for the rollout).
    ai_review_findings: tuple[dict[str, Any], ...] = field(default_factory=tuple)


# ─── Decision ────────────────────────────────────────────────────────────────


def _filter_unhandled(failures: tuple[End2EndFailure, ...], already: frozenset[str]) -> tuple[End2EndFailure, ...]:
    """Drop failures whose idempotency key the orchestrator has already acted on.

    The key is intentionally cheap to compute and stable across processes
    (see :func:`failure_idempotency_key`). The orchestrator stores its
    handled set wherever is most convenient (DB column, K8s label, sticky
    comment marker) and feeds it back in here on the next event.
    """
    out: list[End2EndFailure] = []
    for f in failures:
        key = failure_idempotency_key(f)
        if key in already:
            logger.debug('watcher: skipping already-handled failure %s', key)
            continue
        out.append(f)
    return tuple(out)


def decide_action(ctx: IterationContext) -> IterationDecision:
    """Return what the orchestrator should do given this iteration context.

    Decision precedence (top wins):

    1. **NOOP** — no unhandled failures (or all failures already handled
       per ``already_handled_keys``).
    2. **ESCALATE_MAX_ITERATIONS** — there are real_failure entries AND
       a respawn would push the iteration count to or above
       ``max_iterations``. We must hand off to a human before spawning.
    3. **ESCALATE_REPEATED_INFRA** — ALL unhandled failures are
       preview_infra AND ``prior_infra_count + 1 >= REPEATED_INFRA_THRESHOLD``.
       Two consecutive infra hits on the same PR — cluster's wedged.
    4. **SKIP_INFRA** — all unhandled failures are preview_infra (first
       occurrence on this PR). Post info comment, retest, do not respawn.
    5. **RESPAWN** — at least one unhandled real_failure. Spawn a new
       agent run with feedback_payloads built from the unhandled failures.

    The "all infra" rule (vs "any infra") matters: if a single PR
    surfaces one real_failure and one preview_infra simultaneously, the
    real_failure dominates — we respawn to fix it. The infra failure
    will either clear on the re-run or get its own classification next
    cycle.
    """
    unhandled = _filter_unhandled(ctx.failures, ctx.already_handled_keys)
    if not unhandled:
        return IterationDecision.NOOP

    real_failures = [f for f in unhandled if f.classification == 'real_failure']
    infra_failures = [f for f in unhandled if f.classification == 'preview_infra']

    # Rule 2: max iterations. We treat ``iteration_count >= max_iterations``
    # as "already at or past the ceiling, so a respawn would exceed it".
    # Comparing against the count BEFORE the would-be respawn means a
    # max_iterations=3 budget allows iterations 0, 1, 2 to respawn and
    # iteration 3 to escalate — three real recovery attempts before
    # human triage.
    if real_failures and ctx.iteration_count >= ctx.max_iterations:
        return IterationDecision.ESCALATE_MAX_ITERATIONS

    # Rule 3: repeated infra. Only fires when there are NO real_failures —
    # a real_failure mixed in flips us into respawn (rule 5).
    if infra_failures and not real_failures:
        if ctx.prior_infra_count + 1 >= REPEATED_INFRA_THRESHOLD:
            return IterationDecision.ESCALATE_REPEATED_INFRA
        return IterationDecision.SKIP_INFRA

    # Rule 5 fallthrough — at least one real_failure under the budget.
    return IterationDecision.RESPAWN


# ─── Feedback-payload construction ───────────────────────────────────────────


def _failure_to_payload(failure: End2EndFailure) -> dict[str, Any]:
    """Convert one :class:`End2EndFailure` into the JSON-serialisable
    payload shape carried by ``feedback_payloads``.

    Wraps :meth:`End2EndFailure.to_dict` (which already produces the
    v6p0.5 contract shape) so callers don't have to know about the
    underlying dataclass. Stable here so a future schema change in
    ``End2EndFailure`` doesn't ripple into every consumer of
    ``feedback_payloads``.
    """
    return failure.to_dict()


def build_feedback_payloads(
    failures: tuple[End2EndFailure, ...] | list[End2EndFailure],
    ai_review_findings: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the ``feedback_payloads`` list passed to the spawned agent.

    The shape mirrors the v6p0.5 step-2 contract: a list of plain dicts,
    each with at least a ``kind`` discriminator. The agent's prompt
    construction (see :func:`format_feedback_payloads_for_prompt`)
    dispatches on ``kind`` to render the right per-kind block.

    end2end failures come first so the most-recent infrastructure / app
    failures lead the prompt; ai-review findings (older, less specific)
    follow. Order matters only for human readability; the agent reads
    both blocks unconditionally.
    """
    out: list[dict[str, Any]] = [_failure_to_payload(f) for f in failures]
    if ai_review_findings:
        for finding in ai_review_findings:
            # Defensive normalisation — the ai-review path historically
            # produced payloads with various kind labels. We canonicalise
            # to 'ai_review_finding' here so the prompt template only
            # needs one branch.
            payload = dict(finding)
            payload.setdefault('kind', 'ai_review_finding')
            out.append(payload)
    return out


def _format_end2end_payload(payload: dict[str, Any]) -> list[str]:
    """Render one ``end2end_failure`` payload as prompt-ready markdown.

    Output lines avoid leading whitespace so they slot directly into the
    prompt's top level. The block intentionally tells the agent WHICH gate
    failed, WHAT shape the failure took, and (for UI) gives screenshot URLs
    along with explicit guidance to fetch + read them. Claude can process
    images surfaced via the Read tool when they are on the local FS; the
    agent must `curl`-download URL artifacts to a temp file first.
    """
    lines: list[str] = []
    gate = payload.get('gate', '<unknown gate>')
    classification = payload.get('classification', 'real_failure')
    summary = payload.get('summary', '')
    lines.append(f'### Previous attempt: {gate} ({classification})')
    if summary:
        lines.append(f'**Summary**: {summary}')
    failed_tests = payload.get('failed_tests') or []
    if failed_tests:
        lines.append('')
        lines.append('**Failed tests**:')
        for t in failed_tests:
            name = t.get('name', '<unnamed>')
            msg = t.get('message') or '(no message)'
            lines.append(f'- `{name}` — {msg}')
            if t.get('screenshot_url'):
                lines.append(f'  - Screenshot: {t["screenshot_url"]}')
            if t.get('trace_url'):
                lines.append(f'  - Trace: {t["trace_url"]}')
    artifact_urls = payload.get('artifact_urls') or []
    if artifact_urls:
        lines.append('')
        lines.append('**Artifacts** (download via `curl -L -o /tmp/<name> <url>` then `Read`):')
        for art in artifact_urls:
            spec = art.get('spec_name', '?')
            kind = art.get('kind', '?')
            url = art.get('url', '?')
            cluster = art.get('cluster', '?')
            lines.append(f'- {kind} for `{spec}` ({cluster}): {url}')
    return lines


def _format_ai_review_payload(payload: dict[str, Any]) -> list[str]:
    """Render one ``ai_review_finding`` payload as prompt-ready markdown.

    ai-review payloads are looser-typed than end2end — they originate
    from the existing ai-review verdict path (see
    :mod:`gate.tools.ai_review`) which produces a verdict + score +
    optional reviewer comments. We surface whichever fields are present
    so the agent sees the same information a human reviewer would.
    """
    lines: list[str] = []
    cluster = payload.get('cluster', '?')
    verdict = payload.get('verdict', '?')
    score = payload.get('score', '?')
    lines.append(f'### Previous attempt: AI code review ({cluster}) — {verdict} ({score}/100)')
    if payload.get('summary'):
        lines.append(payload['summary'])
    if payload.get('findings'):
        lines.append('')
        lines.append('**Findings**:')
        for finding in payload['findings']:
            if isinstance(finding, dict):
                file_path = finding.get('file', '<unknown>')
                comment = finding.get('comment', '')
                lines.append(f'- `{file_path}`: {comment}')
            else:
                lines.append(f'- {finding}')
    return lines


def format_feedback_payloads_for_prompt(payloads: list[dict[str, Any]]) -> str:
    """Render the full ``feedback_payloads`` list as one prompt-ready block.

    Returns the empty string when payloads is empty so callers can
    unconditionally include the result with ``f"{block}\n\n{rest}"`` —
    no None-guard needed at the call site.

    The leading header is intentionally clear about what the model
    should do with this section: read the failure details + screenshots,
    THEN proceed with the standard initiative loop. Without that
    framing the agent's habit is to start its loop from step 1 (branch
    check) and read the feedback as ambient context.
    """
    if not payloads:
        return ''
    lines: list[str] = [
        '## Previous-attempt failure context (read this FIRST)',
        '',
        (
            'This is iteration N of a previously-failed initiative run. The watcher '
            "detected actionable failures on the prior attempt's PR and re-spawned you "
            'with the structured failure detail below. Read each block carefully — '
            'screenshots and trace URLs are downloadable via `curl -L -o /tmp/...` '
            'then `Read /tmp/...` — and **fix the cited issues BEFORE re-running the '
            'gate**. The standard initiative loop (branch → edit → push → gate) still '
            'applies; this section just tells you what to fix this cycle.'
        ),
        '',
    ]
    for payload in payloads:
        kind = payload.get('kind', '')
        if kind == 'end2end_failure':
            lines.extend(_format_end2end_payload(payload))
        elif kind == 'ai_review_finding':
            lines.extend(_format_ai_review_payload(payload))
        else:
            # Unknown kind — surface raw JSON so a human reading the
            # prompt can debug. The model is told above to read each
            # block; an unrecognised one is at worst noise, not
            # misleading guidance.
            lines.append(f'### Previous attempt: {kind or "unknown payload"}')
            lines.append(f'```json\n{payload}\n```')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# ─── Idempotency ─────────────────────────────────────────────────────────────


def failure_idempotency_key(
    failure: End2EndFailure,
    pipelinerun_name: str | None = None,
    sha: str | None = None,
) -> str:
    """Return a stable string identifying THIS specific failure observation.

    The key is intentionally derived from fields the orchestrator already
    has in hand at decision time so dedupe lookups are cheap and don't
    require a round-trip to the source-of-truth (Tekton, GitHub).

    Inputs:

    - ``failure`` — the parsed :class:`End2EndFailure` (gate name +
      classification + failed-test names).
    - ``pipelinerun_name`` — the Tekton PipelineRun name when known.
      Optional because some entry points (e.g. unit tests, certain
      retry paths) don't have it; in that case we hash on the failure
      contents alone.
    - ``sha`` — the SHA the gate ran against. Optional for the same
      reason.

    The key is a hex-SHA1 of the concatenated normalized inputs, so it
    fits anywhere (K8s label, DB column, sticky-comment marker) without
    escaping. Hash family choice is SHA-1 because we only need
    collision-resistance among the (PR's-lifetime) handful of failure
    observations, not cryptographic security; sha1's a fast, ubiquitous
    primitive and the keys are not security-sensitive.
    """
    test_names = '|'.join(sorted(t.name for t in failure.failed_tests))
    parts = [
        failure.gate,
        failure.classification,
        test_names,
        pipelinerun_name or '',
        sha or '',
    ]
    raw = '||'.join(parts).encode('utf-8')
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]


# ─── Templated comment / label payloads ──────────────────────────────────────


_MANUAL_FIX_LABEL: Final = 'do-not-merge/manual-fix'
_PREVIEW_INFRA_MARKER: Final = '<!-- leartech-agent-watcher preview_infra -->'
_MANUAL_FIX_MARKER: Final = '<!-- leartech-agent-watcher manual_fix -->'
_REPEATED_INFRA_MARKER: Final = '<!-- leartech-agent-watcher repeated_infra -->'


def manual_fix_label() -> str:
    """The GitHub label the orchestrator applies on max-iterations
    escalation. Exposed as a function (not a constant) so tests reference
    the same source of truth the orchestrator does."""
    return _MANUAL_FIX_LABEL


def manual_fix_comment_body(
    *,
    iteration_count: int,
    max_iterations: int,
    attempts: list[PriorAttempt],
    last_failure: End2EndFailure | None = None,
) -> str:
    """Render the markdown comment posted alongside the manual-fix label.

    Includes the iteration tally, a per-attempt summary, and (when
    available) the most-recent failure shape so the human reviewer can
    triage in one glance. The leading HTML marker is so a future
    de-duplication pass (or a sticky-comment editor) can locate prior
    bodies by string-search rather than full comment parsing.
    """
    lines: list[str] = [
        _MANUAL_FIX_MARKER,
        '## ⚠ Agent iteration ceiling hit — manual fix required',
        '',
        f'The watcher re-spawned the agent {iteration_count} time(s) on this PR '
        f'(ceiling: {max_iterations}). The same kind of failure keeps recurring, '
        'so this PR is now labelled `' + _MANUAL_FIX_LABEL + '` and held pending '
        'human triage.',
        '',
        '### Attempt history',
        '',
    ]
    if not attempts:
        lines.append('_(no recorded attempts — the orchestrator did not surface the per-run record)_')
    else:
        for idx, attempt in enumerate(attempts, 1):
            summary = attempt.summary.strip() or '(no summary)'
            lines.append(f'- **#{idx}** (`{attempt.status}`, started {attempt.started_at}, run_id `{attempt.run_id}`)')
            lines.append(f'  - {summary}')
    if last_failure is not None:
        lines.append('')
        lines.append('### Most recent failure')
        lines.append('')
        lines.append(f'- Gate: `{last_failure.gate}` ({last_failure.classification})')
        lines.append(f'- Summary: {last_failure.summary}')
        if last_failure.failed_tests:
            lines.append('- Failed tests:')
            for t in last_failure.failed_tests:
                lines.append(f'  - `{t.name}` — {t.message or "(no message)"}')
    lines.append('')
    lines.append(
        '_To resume agent iterations after fixing: remove the `'
        + _MANUAL_FIX_LABEL
        + '` label and post `/test end2end` (or `/test all`) on this PR._'
    )
    return '\n'.join(lines) + '\n'


def preview_infra_skip_comment_body(failure: End2EndFailure, retest_command: str = '/test end2end') -> str:
    """Render the info comment posted when the watcher decides ``skip_infra``.

    Stays terse because the most common case is "preview env hadn't come
    up by the time the gate sampled" — the gate will pass on next retest.
    The marker lets the orchestrator's next-cycle dedupe code find a
    prior skip-comment without re-parsing the whole comment body.
    """
    lines = [
        _PREVIEW_INFRA_MARKER,
        '## ℹ Preview-infra failure detected — agent NOT re-spawned',
        '',
        f'The {failure.gate} gate failed with a `preview_infra` shape '
        f'({failure.summary}); this is not actionable from the diff. '
        f'Retrying the gate via `{retest_command}` — the agent will be '
        're-spawned only if the next run produces a real (non-infra) failure.',
        '',
        'Indicative messages from the failed checks:',
    ]
    for t in failure.failed_tests[:3]:  # cap to keep the comment compact
        lines.append(f'- `{t.name}`: {t.message or "(no message)"}')
    if len(failure.failed_tests) > 3:
        lines.append(f'- _…and {len(failure.failed_tests) - 3} more_')
    return '\n'.join(lines) + '\n'


def repeated_infra_escalation_comment_body(
    failure: End2EndFailure,
    *,
    prior_infra_count: int,
) -> str:
    """Render the escalation comment when ``ESCALATE_REPEATED_INFRA`` fires.

    The orchestrator pairs this with the same ``manual-fix`` label so
    cluster operators see the row as ``requires_operator``. Distinct
    marker from the max-iterations escalation so future code can route
    cluster-infra incidents differently from agent-code incidents.
    """
    lines = [
        _REPEATED_INFRA_MARKER,
        '## ⚠ Repeated preview-infra failure — cluster operator escalation',
        '',
        f'The {failure.gate} gate has now failed with `preview_infra` '
        f'{prior_infra_count + 1} time(s) in a row on this PR. This usually '
        'means the preview environment is genuinely wedged (Hydra DNS, '
        'OOMKilled pod, ImagePullBackOff) rather than a transient race.',
        '',
        f'This PR is now labelled `{_MANUAL_FIX_LABEL}` and held pending '
        'cluster-operator triage. The agent will NOT re-spawn automatically.',
        '',
        'Recent failure shape:',
        f'- Gate: `{failure.gate}`',
        f'- Summary: {failure.summary}',
    ]
    if failure.failed_tests:
        lines.append('- Indicative messages:')
        for t in failure.failed_tests[:3]:
            lines.append(f'  - `{t.name}`: {t.message or "(no message)"}')
    return '\n'.join(lines) + '\n'


# Re-exported for callers that want to filter by classification without
# re-importing the module-local classifier (mostly tests + the
# orchestrator's structured logging path).
__all__ = [
    'DEFAULT_MAX_ITERATIONS',
    'End2EndFailure',  # re-export for convenience
    'End2EndTest',  # re-export for convenience
    'IterationContext',
    'IterationDecision',
    'PriorAttempt',
    'REPEATED_INFRA_THRESHOLD',
    'build_feedback_payloads',
    'decide_action',
    'failure_idempotency_key',
    'format_feedback_payloads_for_prompt',
    'manual_fix_comment_body',
    'manual_fix_label',
    'preview_infra_skip_comment_body',
    'repeated_infra_escalation_comment_body',
]
