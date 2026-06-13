"""Iteration-loop watcher — decides what to do when gate failures arrive.

Step 2 of 3 of the v6p0.5 plan. Step 1 (``gate.tools.end2end_gate``)
fetches + classifies the failure; this package wraps the
"should we re-spawn the agent / post an info comment / escalate?"
decision around those payloads and the existing ai-review feedback
shape.

Public surface lives in :mod:`gate.watcher.iteration_loop`.
"""

from gate.watcher.artefact_dispatch import ArtefactFetcher, dispatch_structured_failure
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
    'ArtefactFetcher',
    'IterationContext',
    'IterationDecision',
    'PriorAttempt',
    'build_feedback_payloads',
    'decide_action',
    'dispatch_structured_failure',
    'failure_idempotency_key',
    'format_feedback_payloads_for_prompt',
    'manual_fix_comment_body',
    'preview_infra_skip_comment_body',
]
