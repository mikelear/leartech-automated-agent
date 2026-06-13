"""Step-aware classification of Tekton pipeline failures.

Phase G.2. Pairs with the `leartech-tekton` MCP server (G.1, in
``gate/mcp_servers/tekton.py``). The MCP server tells the agent
WHICH step failed (git-clone vs ruff vs pytest vs kaniko vs …); this
module tells the agent WHAT TO DO ABOUT IT.

## The problem this solves

Before G.2 the agent saw only "lint: failure" as the aggregate
GitHub-side status. Two genuinely different root-causes — a
``ruff format`` violation vs a hidden ``git`` merge conflict during the
``git-clone`` step — look identical from that vantage point. The agent's
canonical "retry then escalate" path then burns a full Tekton cycle
(~10–30 min) on each retry without addressing the cause. D.5.1.2 spent
$12.30 on exactly this shape: a merge conflict masquerading as a lint
failure.

The classification matrix below buckets each canonical failure shape
to an action verb the agent can dispatch on:

- ``rebase``    — pull main + rebase + force-push-with-lease
- ``fix_code``  — edit the cited file + recommit (the agent's normal path)
- ``fix_test``  — edit the test + recommit (a narrower fix_code)
- ``retry``     — ``/test <check>`` chatops; the failure is transient
- ``escalate``  — human needed; post sticky and stop

## v6p0.6 step 2 extensions

The original matrix routed govulncheck advisories, dynamic-scan SARIF
findings, and Helm preview-deploy errors all into the same two generic
buckets (``security_scan_finding`` → escalate, ``preview_deploy_failure``
→ escalate). In practice each of those gates has subclasses the agent
CAN action:

- ``govulncheck_vulnerability`` → fix_code (module bump / pinned upgrade)
- ``dynamic_scan_high_finding`` → fix_code (HIGH/CRITICAL SARIF findings
  are usually code-level — input validation, auth headers)
- ``dynamic_scan_low_finding``  → escalate (LOW/INFO are noise — classify
  and hand off rather than churn)
- ``helm_missing_value``        → fix_code (mistyped key / unset value in
  the chart values.yaml — the agent can patch)
- ``helm_missing_secret``       → escalate (operator must seed the
  Secret; outside the agent's reach)
- ``helm_timeout``              → retry (transient rollout race; ``/test``
  the preview check)

The new shapes are inserted BEFORE the existing generic catch-alls so the
first-match-wins ordering dispatches to the more-actionable bucket. The
generic shapes remain as fallbacks when none of the subclasses fire.

The "text-pattern path" implemented here covers the case where the
artefact is rendered inline in the Tekton step log (govulncheck's default
text mode; ZAP's textual summary; Helm's `INSTALLATION FAILED` stderr).
Structured artefact ingestion (SARIF JSON, govulncheck `-json`) is the
companion mechanism in v6p0.6 step 1 (`gate/tools/artefact_parsers.py`);
both feeds end up in the same dispatcher. Either signal is sufficient on
its own — patterns are over-cautious by design.

## Design choices

1. **Pure heuristics over LLM classification.** Each canonical pattern
   is a short list of substrings that appear in real Tekton step logs
   from this repo + leartech-llm-training-data. Heuristics are O(n) on
   ~200 lines of log and zero token cost; the agent invokes this once
   per failed step. An LLM-based fallback could come later but isn't
   needed today — the matrix covers 95%+ of observed failures.

2. **Step-name biased.** A "git-clone" step failing with "CONFLICT" is
   a merge conflict, full stop. A "ruff" step failing with "would be
   reformatted" is a format error, even if the log incidentally
   mentions "test" elsewhere. Step name is the strong prior; log
   substrings disambiguate within that prior.

3. **First-match wins, ordered specific → general.** The heuristic
   list runs in order; the first match returns. ``unknown`` is the
   catch-all and always escalates — the agent never blindly retries an
   unrecognised failure shape.

4. **Frozen dataclass output.** The agent receives an immutable
   ``StepFailure`` it can serialise back through the MCP layer without
   worrying about state aliasing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Final

# ─── Action verbs ─────────────────────────────────────────────────────────────

# The action the initiative loop dispatches on. Kept as plain strings (not an
# Enum) because the MCP layer serialises them as JSON and the agent reasons
# about them as text.
ACTION_REBASE: Final = 'rebase'
ACTION_FIX_CODE: Final = 'fix_code'
ACTION_FIX_TEST: Final = 'fix_test'
ACTION_RETRY: Final = 'retry'
ACTION_ESCALATE: Final = 'escalate'

VALID_ACTIONS: Final = frozenset({ACTION_REBASE, ACTION_FIX_CODE, ACTION_FIX_TEST, ACTION_RETRY, ACTION_ESCALATE})


# ─── Classification → action map (the spec's CLASSIFICATIONS table) ──────────

CLASSIFICATIONS: Final[dict[str, str]] = {
    'git_merge_conflict': ACTION_REBASE,
    'ruff_format_error': ACTION_FIX_CODE,
    'ruff_lint_error': ACTION_FIX_CODE,
    'mypy_type_error': ACTION_FIX_CODE,
    'pytest_test_failure': ACTION_FIX_TEST,
    'kaniko_build_failure': ACTION_ESCALATE,
    'image_pull_backoff': ACTION_ESCALATE,
    'ai_review_red_finding': ACTION_FIX_CODE,
    'tekton_step_oom': ACTION_ESCALATE,
    'tekton_step_timeout': ACTION_RETRY,
    'preview_deploy_failure': ACTION_ESCALATE,
    'security_scan_finding': ACTION_ESCALATE,
    # ─ v6p0.6 step 2 additions — more specific shapes than the generic
    # ``security_scan_finding`` / ``preview_deploy_failure`` catch-alls. ─
    'govulncheck_vulnerability': ACTION_FIX_CODE,
    'dynamic_scan_high_finding': ACTION_FIX_CODE,
    'dynamic_scan_low_finding': ACTION_ESCALATE,
    'helm_missing_value': ACTION_FIX_CODE,
    'helm_missing_secret': ACTION_ESCALATE,
    'helm_timeout': ACTION_RETRY,
    'unknown': ACTION_ESCALATE,
}


# ─── Heuristic definitions ───────────────────────────────────────────────────


@dataclass(frozen=True)
class _Heuristic:
    """One classification's detection inputs.

    A heuristic matches when EITHER (any step-name substring matches AND any
    log substring matches) OR a `must_match_in_log` regex hits unambiguously.
    Step-name match alone is never sufficient (a 'lint' step can fail for
    OOM, etc.); the log content is the authoritative signal.
    """

    classification: str
    step_name_substrings: tuple[str, ...] = field(default_factory=tuple)
    log_substrings: tuple[str, ...] = field(default_factory=tuple)
    log_regexes: tuple[re.Pattern[str], ...] = field(default_factory=tuple)


# Heuristics in priority order — first match wins. Reordering changes
# behaviour for ambiguous logs that match multiple patterns (e.g. a pytest
# step that OOMs mid-run could match both ``tekton_step_oom`` and
# ``pytest_test_failure``; OOM wins because it's the root cause).
_HEURISTICS: tuple[_Heuristic, ...] = (
    # ─ OOM kills + cluster timeouts — must come first; they can occur in
    # any step and the root cause is the resource, not the step's code. ─
    _Heuristic(
        classification='tekton_step_oom',
        log_substrings=(
            'OOMKilled',
            'exit code 137',
            'exit code: 137',
            'memory limit exceeded',
            'Out of memory',
        ),
    ),
    _Heuristic(
        classification='tekton_step_timeout',
        log_substrings=(
            'DeadlineExceeded',
            'TaskRunTimeout',
            'PipelineRunTimeout',
            'context deadline exceeded',
            'step exceeded its timeout',
        ),
    ),
    _Heuristic(
        classification='image_pull_backoff',
        log_substrings=(
            'ImagePullBackOff',
            'ErrImagePull',
            'manifest unknown',
            'pull access denied',
            'unauthorized: authentication required',
        ),
    ),
    # ─ Step-specific shapes ──────────────────────────────────────────────
    _Heuristic(
        classification='git_merge_conflict',
        step_name_substrings=('git-clone', 'git-merge', 'clone', 'merge'),
        log_substrings=(
            'CONFLICT (content)',
            'Merge conflict in',
            'Automatic merge failed',
            'fix conflicts and run',
            'You have unmerged paths',
            'needs merge',
        ),
    ),
    _Heuristic(
        classification='ruff_format_error',
        step_name_substrings=('ruff', 'lint', 'format'),
        log_substrings=(
            'Would reformat:',
            'would be reformatted',
            'files would be reformatted',
            'ruff format',
        ),
    ),
    _Heuristic(
        classification='ruff_lint_error',
        step_name_substrings=('ruff', 'lint'),
        log_substrings=(
            'ruff check',
            'Found ',  # "Found 3 errors." — paired with regex below
        ),
        log_regexes=(
            # Canonical ruff diagnostic: `path.py:12:5: E501 line too long`.
            # Use multiline to bind ^ to line starts (a casual mention of
            # "E501" in prose elsewhere wouldn't match).
            re.compile(r'^[\w./\-]+\.py:\d+:\d+:\s+[A-Z]\d{2,4}\b', re.MULTILINE),
        ),
    ),
    _Heuristic(
        classification='mypy_type_error',
        step_name_substrings=('mypy', 'typecheck', 'type-check'),
        log_substrings=(
            'error:',
            'Found ',  # "Found 3 errors in 2 files"
        ),
        log_regexes=(
            # `path.py:12: error: Incompatible types...` — mypy's canonical shape.
            re.compile(r'^[\w./\-]+\.py:\d+:\s+error:', re.MULTILINE),
        ),
    ),
    _Heuristic(
        classification='pytest_test_failure',
        step_name_substrings=('pytest', 'test', 'unit'),
        log_substrings=(
            '= FAILURES =',
            'short test summary info',
            'FAILED tests/',
            'AssertionError',
            'failed,',
            'errors during collection',
        ),
    ),
    _Heuristic(
        classification='ai_review_red_finding',
        step_name_substrings=('ai-review', 'ai_review', 'review'),
        log_substrings=(
            'review_verdict: red',
            'verdict: red',
            'MUST FIX',
            'must-fix',
            'BLOCKING:',
            'red finding',
        ),
    ),
    # ─ v6p0.6 step 2: specific govulncheck shape — must come BEFORE the
    # generic ``security_scan_finding`` so Go vulnerability advisories route
    # to ``fix_code`` (bump module) rather than ``escalate``. The agent can
    # actually fix these (renovate-style bump, pinned upgrade), whereas an
    # unknown CVE in a base image generally needs human attention. ─
    _Heuristic(
        classification='govulncheck_vulnerability',
        step_name_substrings=(
            'govulncheck',
            'vulncheck',
            'go-vuln',
            'go-vulncheck',
        ),
        log_substrings=(
            'Vulnerability #',
            'Your code is affected by',
            'More info: https://pkg.go.dev/vuln/',
        ),
        log_regexes=(
            # govulncheck advisory IDs are stable: GO-YYYY-NNNN.
            re.compile(r'\bGO-\d{4}-\d{3,6}\b'),
        ),
    ),
    # ─ v6p0.6 step 2: dynamic-scan severity split. HIGH/CRITICAL findings
    # are typically code-level fixes (input validation, auth header
    # tightening) the agent CAN attempt; LOW/INFORMATIONAL findings are
    # noise the agent should classify and hand off rather than churn on. ─
    _Heuristic(
        classification='dynamic_scan_high_finding',
        step_name_substrings=('dynamic-scan', 'dast', 'zap', 'nuclei'),
        log_substrings=(
            # SARIF severity tokens (the structured shape step 1 reads;
            # the text form is what falls into the step log).
            '"level": "error"',
            '"level":"error"',
            'severity: high',
            'severity: critical',
            'Risk: High',
            'Risk: Critical',
            # ZAP textual summary lines.
            'High (Medium):',
            'Critical (',
        ),
    ),
    _Heuristic(
        classification='dynamic_scan_low_finding',
        step_name_substrings=('dynamic-scan', 'dast', 'zap', 'nuclei'),
        log_substrings=(
            '"level": "note"',
            '"level":"note"',
            '"level": "warning"',
            '"level":"warning"',
            'severity: low',
            'severity: informational',
            'Risk: Low',
            'Risk: Informational',
            'Low (Medium):',
            'Informational (',
        ),
    ),
    _Heuristic(
        classification='security_scan_finding',
        step_name_substrings=('security-scan', 'image-scan', 'trivy', 'grype', 'dynamic-scan'),
        log_substrings=(
            'CRITICAL: ',
            'HIGH: ',
            'vulnerabilities found',
            'Total: ',  # trivy summary header — paired with CRITICAL/HIGH
            'CVE-',
        ),
    ),
    _Heuristic(
        classification='kaniko_build_failure',
        step_name_substrings=('kaniko', 'build', 'image', 'docker'),
        log_substrings=(
            'executor failed running',
            'error building image',
            'kaniko encountered an error',
            'failed to build:',
            'COPY failed',
            'RUN failed',
        ),
    ),
    # ─ v6p0.6 step 2: Helm-specific subclasses of preview_deploy_failure.
    # Order matters: the missing-secret pattern checks for "Secret"
    # explicitly so it routes to escalate (operator must seed), while the
    # missing-value pattern catches mistyped keys / unset values the agent
    # can fix in the chart, and the timeout pattern catches transient
    # rollout races worth retesting. Generic preview_deploy_failure stays
    # as the catch-all below. ─
    _Heuristic(
        classification='helm_missing_secret',
        step_name_substrings=('preview', 'helm', 'promote', 'deploy'),
        log_substrings=(
            'secrets "',  # e.g. `secrets "foo-creds" not found`
            'Secret "',
            'secret not found',
            'could not find secret',
            'MountVolume.SetUp failed for volume',
            'secret reference',
        ),
    ),
    _Heuristic(
        classification='helm_timeout',
        step_name_substrings=('preview', 'helm', 'promote', 'deploy'),
        log_substrings=(
            'timed out waiting for the condition',
            'Error: timed out waiting',
            'context deadline exceeded',  # Helm flavoured
            'release ready timeout',
            'rollout status timed out',
        ),
    ),
    _Heuristic(
        classification='helm_missing_value',
        step_name_substrings=('preview', 'helm', 'promote', 'deploy'),
        log_substrings=(
            'execution error at',  # Helm template error preamble
            'nil pointer evaluating',
            'map has no entry for key',
            'at <.Values.',
            'required value',
            'missing required key',
            'coalesce.go',
            'no template',
            'YAML parse error',
        ),
    ),
    _Heuristic(
        classification='preview_deploy_failure',
        step_name_substrings=('preview', 'helm', 'promote', 'deploy'),
        log_substrings=(
            'release failed',
            'helm upgrade',
            'INSTALLATION FAILED',
            'UPGRADE FAILED',
            'no matches for kind',
            'failed to deploy',
            'preview environment',
        ),
    ),
)


# ─── Output dataclass ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepFailure:
    """One Tekton step's diagnosed failure.

    Fields mirror the spec's pseudocode. ``classification`` is one of the
    ``CLASSIFICATIONS`` keys; ``action`` is the corresponding value from
    that map (one of the ``ACTION_*`` constants).

    ``log_tail`` is preserved on the dataclass so the agent (or a downstream
    sticky-comment writer) can include the relevant lines in a PR comment
    without re-fetching from the cluster. The classifier itself doesn't
    trim the input — that's the caller's responsibility (the MCP layer
    caps it at ~200 lines via ``step_logs``).
    """

    pipelinerun: str
    step_name: str
    log_tail: str
    classification: str
    action: str

    def to_dict(self) -> dict[str, str]:
        """JSON-serialisable representation for MCP tool output."""
        return {
            'pipelinerun': self.pipelinerun,
            'step_name': self.step_name,
            'log_tail': self.log_tail,
            'classification': self.classification,
            'action': self.action,
        }


# ─── Classifier ──────────────────────────────────────────────────────────────


def _step_name_matches(step_name: str, substrings: tuple[str, ...]) -> bool:
    """True when any substring appears in `step_name` (case-insensitive).

    Empty `substrings` returns True — the heuristic is step-name-agnostic
    (e.g. OOM, which can hit any step).
    """
    if not substrings:
        return True
    lower = step_name.lower()
    return any(s.lower() in lower for s in substrings)


def _log_matches(log_tail: str, heuristic: _Heuristic) -> bool:
    """True when any of the heuristic's substrings or regexes fires."""
    if heuristic.log_substrings:
        for needle in heuristic.log_substrings:
            if needle in log_tail:
                return True
    if heuristic.log_regexes:
        for pattern in heuristic.log_regexes:
            if pattern.search(log_tail):
                return True
    return False


def classify_step_failure(
    step_name: str,
    log_tail: str,
    *,
    pipelinerun: str = '',
) -> StepFailure:
    """Diagnose one failed Tekton step.

    Walks the ordered ``_HEURISTICS`` list and returns the first match. If
    nothing matches, returns the ``unknown`` classification with the
    ``escalate`` action — the agent must NOT retry an unrecognised
    failure shape blindly.

    ``log_tail`` should be the last ~100-200 lines of stderr+stdout from
    the failed step (what ``leartech-tekton.step_logs`` already produces
    by default). Empty log → ``unknown``; we never guess from step-name
    alone because the same step can fail for different reasons.

    Returns a frozen ``StepFailure``. Caller is responsible for passing
    it on through the action dispatcher.
    """
    if not log_tail.strip():
        # Empty log — pod GC'd or step never ran. Treat as unknown so the
        # agent escalates rather than guessing.
        return StepFailure(
            pipelinerun=pipelinerun,
            step_name=step_name,
            log_tail=log_tail,
            classification='unknown',
            action=ACTION_ESCALATE,
        )

    for heuristic in _HEURISTICS:
        if not _step_name_matches(step_name, heuristic.step_name_substrings):
            continue
        if not _log_matches(log_tail, heuristic):
            continue
        return StepFailure(
            pipelinerun=pipelinerun,
            step_name=step_name,
            log_tail=log_tail,
            classification=heuristic.classification,
            action=CLASSIFICATIONS[heuristic.classification],
        )

    return StepFailure(
        pipelinerun=pipelinerun,
        step_name=step_name,
        log_tail=log_tail,
        classification='unknown',
        action=ACTION_ESCALATE,
    )


def diagnose_failures(steps: list[dict[str, object]]) -> list[StepFailure]:
    """Diagnose every failed step in a list of ``step_status`` rows.

    Convenience helper that filters rows to the failed subset and calls
    ``classify_step_failure`` on each. The agent typically calls this with
    the output of ``step_status`` after fetching ``step_logs`` for each.

    Each input row must carry: ``step`` (name), ``state``, and the
    pre-fetched log tail under either ``log_tail`` or ``logs``. Rows whose
    state isn't ``Failed`` are skipped — only failures need diagnosis.
    The ``pipelinerun`` key is propagated through to the output if present.
    """
    out: list[StepFailure] = []
    for row in steps:
        if str(row.get('state', '')) != 'Failed':
            continue
        log_tail = str(row.get('log_tail') or row.get('logs') or '')
        out.append(
            classify_step_failure(
                step_name=str(row.get('step', '')),
                log_tail=log_tail,
                pipelinerun=str(row.get('pipelinerun', '')),
            )
        )
    return out


def summarise_dispatch(failures: list[StepFailure]) -> str:
    """Pick the dispatch action for a set of failed steps.

    Decision table for a *set* of failures (mirrors the spec pseudocode):

    - all failures are ``rebase``       → ``rebase``
    - any failure is ``fix_code``       → ``fix_code`` (precedence: an
      actual code fix supersedes a transient retry)
    - any failure is ``fix_test``       → ``fix_test``
    - all failures are ``retry``        → ``retry``
    - otherwise (mixed or any escalate) → ``escalate``

    Empty list → ``escalate`` (no failure but called anyway is a bug
    upstream; surface it rather than silently passing).
    """
    if not failures:
        return ACTION_ESCALATE
    actions = {f.action for f in failures}
    if actions == {ACTION_REBASE}:
        return ACTION_REBASE
    if ACTION_FIX_CODE in actions:
        return ACTION_FIX_CODE
    if ACTION_FIX_TEST in actions:
        return ACTION_FIX_TEST
    if actions == {ACTION_RETRY}:
        return ACTION_RETRY
    return ACTION_ESCALATE


def failures_to_json(failures: list[StepFailure]) -> str:
    """Render a list of StepFailures as a JSON string (for MCP transport)."""
    return json.dumps([f.to_dict() for f in failures], indent=2)
