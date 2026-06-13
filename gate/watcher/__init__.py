"""Iteration-loop watcher — decides what to do when gate failures arrive.

Step 2 of 3 of the v6p0.5 plan. Step 1 (``gate.tools.end2end_gate``)
fetches + classifies the failure; this package wraps the
"should we re-spawn the agent / post an info comment / escalate?"
decision around those payloads and the existing ai-review feedback
shape.

Public surface lives in :mod:`gate.watcher.iteration_loop` (end2end
gate dispatch) and :mod:`gate.watcher.ai_review_iteration` (v6p0.6
step 4 — auto-iterate on ai-review red findings).
"""

from gate.watcher.ai_review_iteration import (
    SCORE_CONFIDENCE_THRESHOLD,
    AIReviewDecision,
    AIReviewIterationContext,
    build_ai_review_failure_payload,
    build_ai_review_failure_payloads,
    decide_ai_review_action,
    format_ai_review_failure_payload,
    verdict_idempotency_key,
)
from gate.watcher.artefact_dispatch import ArtefactFetcher, dispatch_structured_failure
from gate.watcher.escalation import (
    DEFAULT_CROSS_GATE_CAP,
    DEFAULT_SAME_ERROR_THRESHOLD,
    MANUAL_RETRY_COMMAND,
    AttemptHistory,
    EscalationReason,
    compute_fingerprint,
    escalation_comment_body,
    escalation_label,
    is_manual_retry_command,
    mark_gate_passed,
    record_attempt,
    reset_all,
    should_escalate,
)
from gate.watcher.iteration_loop import (
    IterationContext,
    IterationDecision,
    PriorAttempt,
    build_feedback_payloads,
    decide_action,
    failure_idempotency_key,
    format_feedback_payloads_for_prompt,
    manual_fix_comment_body,
    preview_infra_skip_comment_body,
)

__all__ = [
    'AIReviewDecision',
    'AIReviewIterationContext',
    'ArtefactFetcher',
    'AttemptHistory',
    'DEFAULT_CROSS_GATE_CAP',
    'DEFAULT_SAME_ERROR_THRESHOLD',
    'EscalationReason',
    'IterationContext',
    'IterationDecision',
    'MANUAL_RETRY_COMMAND',
    'PriorAttempt',
    'SCORE_CONFIDENCE_THRESHOLD',
    'build_ai_review_failure_payload',
    'build_ai_review_failure_payloads',
    'build_feedback_payloads',
    'compute_fingerprint',
    'decide_action',
    'decide_ai_review_action',
    'dispatch_structured_failure',
    'escalation_comment_body',
    'escalation_label',
    'failure_idempotency_key',
    'format_ai_review_failure_payload',
    'format_feedback_payloads_for_prompt',
    'is_manual_retry_command',
    'manual_fix_comment_body',
    'mark_gate_passed',
    'preview_infra_skip_comment_body',
    'record_attempt',
    'reset_all',
    'should_escalate',
    'verdict_idempotency_key',
]
