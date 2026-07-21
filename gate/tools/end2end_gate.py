"""End-to-end gate output parser + classifier.

The PR watcher consumes AI-review verdicts (see ``ai_review.py``) and Playwright
artefact stickies (see ``playwright_artifacts.py``) today, but **ignores** the
structured output produced by the ``end2end`` (backend) and ``end2end-ui`` (UI)
gates. That gap was surfaced 2026-06-13 via memory
``feedback_agent_must_read_and_extend_end2end_gates``: when those gates fail
the agent has no idea WHY and cannot feed the failure detail back into its
iteration loop.

This module closes the gap for the *fetch + parse + classify* half (v6p0.5
step 1 of 3). The next two slices will wire the structured payload returned
by :func:`build_end2end_failure` into the iteration mechanism and the
standards/calibration enforcement layer.

## What end2end gates produce

The catalog's ``tasks/end2end/pullrequest.yaml`` runs a battery of shell
checks against the deployed preview, then dumps a ``results.json`` block to
its final step's stdout. The canonical shape (observed 2026-06-13 on
``leartech-auth-service`` PR #58) is::

    {
      "success": false,
      "summary": "1/4 checks passed",
      "tests": [
        {"name": "00-seed-test-data", "status": "pass", ...},
        {"name": "01-smoke",          "status": "fail",
         "message": "GET /health/live HTTP 000 FAIL"},
        ...
      ]
    }

``end2end-ui`` follows the same JSON envelope but additionally posts a
Playwright sticky comment containing artefact URLs (screenshots, videos,
traces) — those are parsed by :mod:`gate.tools.playwright_artifacts` already;
this module *composes* with it rather than re-parsing.

## Classification

Two distinct failure shapes call for two distinct agent responses:

- ``real_failure`` — a test ran and failed with an assertion-style outcome
  (HTTP 4xx/5xx, body mismatch, wrong DOM state, etc.). The agent SHOULD
  iterate: the failure cites application behaviour the diff is responsible
  for.
- ``preview_infra`` — the preview deployment never came up, DNS didn't
  resolve, or the test never reached the application. Canonical signal:
  ``HTTP 000`` (curl's exit-code marker for "no response at all"),
  ``connection refused``, ``getaddrinfo``, etc. The agent must NOT iterate:
  the failure has nothing to do with the diff; the right action is to
  classify as infra in the sticky and retest via ``/test end2end``.

The taxonomy mirrors the existing ai-review pattern (warning vs blocking)
and is deliberately conservative: when in doubt, we say ``real_failure`` and
let the agent iterate — false-positive iterations cost a Tekton cycle;
false-negative iterations would silently skip real bugs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from gate.tools.playwright_artifacts import Artifact

logger = logging.getLogger(__name__)


# ─── Gate detection ──────────────────────────────────────────────────────────


_CLUSTER_PREFIXES: Final = ('gcp/', 'az/')
_END2END_CHECK_NAMES: Final = frozenset({'end2end', 'end2end-ui'})


def _strip_cluster_prefix(check_name: str) -> str:
    """Drop a leading ``gcp/`` or ``az/`` so ``gcp/end2end`` → ``end2end``."""
    for prefix in _CLUSTER_PREFIXES:
        if check_name.startswith(prefix):
            return check_name[len(prefix) :]
    return check_name


def is_end2end_gate(check_name: str) -> bool:
    """Recognise an ``end2end`` or ``end2end-ui`` check, with or without cluster prefix.

    Accepts: ``end2end``, ``end2end-ui``, ``gcp/end2end``, ``az/end2end``,
    ``gcp/end2end-ui``, ``az/end2end-ui``. Anything else is False — we don't
    fuzzy-match here because ``end2end-foo`` would surprise the agent later.
    """
    return _strip_cluster_prefix(check_name) in _END2END_CHECK_NAMES


def is_end2end_ui_gate(check_name: str) -> bool:
    """True iff the check is the UI-flavoured end2end gate (Playwright)."""
    return _strip_cluster_prefix(check_name) == 'end2end-ui'


# ─── results.json parsing ────────────────────────────────────────────────────


# The harness dumps a results.json block somewhere in the step's stdout. It is
# not necessarily the only JSON object in the log — `kubectl logs` may include
# kubelet metadata, the wrapper script may echo other JSON envelopes, etc.
# We scan for the FIRST top-level object whose keys include `tests` AND either
# `success` or `summary`. That's specific enough to avoid the common false-
# positives (Tekton step metadata, helm release JSON) without being too brittle.
# Python's `re` lacks recursive matching; we hand-roll a balanced-brace walker
# in `_iter_balanced_json_objects` instead.


def _iter_balanced_json_objects(text: str) -> list[str]:
    """Yield every top-level balanced ``{...}`` substring in ``text``.

    Skips brace-pairs nested inside strings (single backslash escape support
    is enough for JSON). Returns a list rather than a generator so callers
    can re-scan it cheaply.
    """
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] != '{':
            i += 1
            continue
        depth = 0
        start = i
        in_string = False
        escape = False
        while i < n:
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        out.append(text[start : i + 1])
                        i += 1
                        break
            i += 1
        else:
            # Hit EOF mid-object — give up on this candidate.
            break
    return out


def parse_results_json_from_log(log_tail: str) -> dict[str, Any] | None:
    """Extract the harness's ``results.json`` payload from a step's log tail.

    Returns the parsed dict, or ``None`` when no recognisable block is found.
    A block is recognised when its top-level keys include ``tests`` (the list
    of per-check results) AND one of ``success`` / ``summary``.

    Robust to:
    - Surrounding kubectl / Tekton metadata in the log.
    - Other JSON objects in the log (helm release info, kubectl yaml-as-json).
    - The dump being indented / pretty-printed across many lines.
    """
    if not log_tail:
        return None
    for candidate in _iter_balanced_json_objects(log_tail):
        try:
            doc = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if 'tests' not in doc:
            continue
        if 'success' not in doc and 'summary' not in doc:
            continue
        if not isinstance(doc.get('tests'), list):
            continue
        return doc
    return None


# ─── Per-test dataclass + extraction ─────────────────────────────────────────


@dataclass(frozen=True)
class End2EndTest:
    """One row from the harness's ``tests`` list, narrowed to what we use."""

    name: str
    status: str  # 'pass' | 'fail' | 'skip' (or any harness-specific token)
    message: str | None = None
    trace_url: str | None = None
    screenshot_url: str | None = None

    @property
    def failed(self) -> bool:
        return self.status.lower() == 'fail'


def extract_failed_tests(results: dict[str, Any]) -> list[End2EndTest]:
    """Return the failed-test rows from a parsed ``results.json`` dict.

    Defensive: each row may be missing keys (older harnesses), so we coerce
    via ``.get``. Non-dict rows are skipped silently — we never want this
    parser to raise on a malformed log.
    """
    failed: list[End2EndTest] = []
    for row in results.get('tests', []) or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get('status', '') or '').lower()
        if status != 'fail':
            continue
        failed.append(
            End2EndTest(
                name=str(row.get('name', '') or '<unknown>'),
                status=status,
                message=row.get('message') if row.get('message') else None,
                trace_url=row.get('trace_url') if row.get('trace_url') else None,
                screenshot_url=row.get('screenshot_url') if row.get('screenshot_url') else None,
            )
        )
    return failed


# ─── Classification ──────────────────────────────────────────────────────────


# Substring tokens that — when found in a failure's message OR the
# surrounding log tail — indicate the preview never came up rather than the
# app misbehaving. Curl reports HTTP 000 when it never got a response at all;
# the other tokens cover DNS / TCP / preview-gate timing shapes we've seen.
_PREVIEW_INFRA_TOKENS: Final = (
    'HTTP 000',
    'http 000',
    'Connection refused',
    'connection refused',
    'No route to host',
    'no route to host',
    'Name or service not known',
    'name or service not known',
    'Could not resolve host',
    'could not resolve host',
    'getaddrinfo',
    'EAI_AGAIN',
    'preview-gate timed out',
    'preview not ready',
    'preview deployment never came up',
    'preview env never ready',
    'DNS lookup failed',
)


def _classify_message(text: str) -> str | None:
    """Inspect a single string for a preview-infra signal. Returns the matching token, or None."""
    if not text:
        return None
    for token in _PREVIEW_INFRA_TOKENS:
        if token in text:
            return token
    return None


def classify_end2end_failure(results: dict[str, Any] | None, log_tail: str) -> str:
    """Decide whether a failure is ``real_failure`` or ``preview_infra``.

    Decision rule:
    1. If we have a parsed results dict, scan every failed test's message for a
       preview-infra token. If the failures are *all* preview-infra-shaped, the
       gate failure is ``preview_infra``.
    2. If the parsed dict has failures with non-infra messages mixed in, the
       overall classification is ``real_failure`` (some tests genuinely ran).
    3. If no results dict at all (e.g. step crashed before dumping), fall back
       to scanning the log tail for preview-infra tokens. Hit → preview_infra;
       miss → real_failure.

    Conservative when ambiguous: we'd rather have the agent iterate on a
    suspected-real failure than walk away from a real bug we misclassified.
    """
    if results is not None:
        failed_tests = extract_failed_tests(results)
        if failed_tests:
            non_infra = [t for t in failed_tests if _classify_message(t.message or '') is None]
            if not non_infra:
                return 'preview_infra'
            return 'real_failure'
        # Parsed but no failures — odd path (gate said FAILURE but harness says
        # all green). Treat as real_failure so the agent looks at it rather than
        # auto-retesting.
        return 'real_failure'
    # No parsed results — fall back to scanning the raw log.
    if _classify_message(log_tail) is not None:
        return 'preview_infra'
    return 'real_failure'


# ─── Failure payload ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class End2EndFailure:
    """Structured payload describing a failed end2end / end2end-ui gate.

    Shape mirrors the JSON contract specified in the v6p0.5 initiative:

        {
          "kind": "end2end_failure",
          "gate": "az/end2end" | ...,
          "classification": "real_failure" | "preview_infra",
          "summary": "1/4 checks passed",
          "failed_tests": [
            {"name": "...", "message": "...", "trace_url": null,
             "screenshot_url": "..." (UI only)}
          ],
          "actionable": true
        }
    """

    gate: str  # full check name, cluster-prefixed (e.g. 'az/end2end-ui')
    classification: str  # 'real_failure' | 'preview_infra'
    summary: str
    failed_tests: tuple[End2EndTest, ...] = field(default_factory=tuple)
    actionable: bool = True
    # UI-only: when end2end-ui artefacts (screenshots, videos, traces) are
    # available from the parallel playwright sticky, we attach the URLs here
    # so downstream consumers don't have to re-parse the sticky comment.
    artifact_urls: tuple[Artifact, ...] = field(default_factory=tuple)

    @property
    def is_ui(self) -> bool:
        return is_end2end_ui_gate(self.gate)

    def to_dict(self) -> dict[str, Any]:
        """Render the JSON-serialisable contract payload."""

        def _test_dict(t: End2EndTest) -> dict[str, Any]:
            return {
                'name': t.name,
                'message': t.message,
                'trace_url': t.trace_url,
                'screenshot_url': t.screenshot_url,
            }

        out: dict[str, Any] = {
            'kind': 'end2end_failure',
            'gate': self.gate,
            'classification': self.classification,
            'summary': self.summary,
            'failed_tests': [_test_dict(t) for t in self.failed_tests],
            'actionable': self.actionable,
        }
        if self.artifact_urls:
            out['artifact_urls'] = [
                {'spec_name': a.spec_name, 'kind': a.kind, 'url': a.url, 'cluster': a.cluster}
                for a in self.artifact_urls
            ]
        return out


def _annotate_failed_tests_with_artifacts(
    failed: list[End2EndTest], artifacts: tuple[Artifact, ...]
) -> tuple[End2EndTest, ...]:
    """Best-effort: attach screenshot/trace URLs to failed tests when the spec name matches.

    Playwright artefact spec_name encodes the spec + first-test slug (see
    ``playwright_artifacts.PlaywrightRun``); the harness's results.json
    ``name`` is typically the *spec* name. We match conservatively on a
    prefix-or-suffix substring to handle both encodings without over-matching.
    """
    if not artifacts:
        return tuple(failed)
    by_spec: dict[str, dict[str, str]] = {}
    for art in artifacts:
        bucket = by_spec.setdefault(art.spec_name, {})
        bucket[art.kind] = art.url
    out: list[End2EndTest] = []
    for t in failed:
        # Match: exact spec_name, or the artefact's spec_name starts with the
        # test's name (artefacts often append a test-slug to the spec name).
        match_key = None
        for spec_name in by_spec:
            if spec_name == t.name or spec_name.startswith(t.name + '-') or t.name in spec_name:
                match_key = spec_name
                break
        if match_key is None:
            out.append(t)
            continue
        bucket = by_spec[match_key]
        out.append(
            End2EndTest(
                name=t.name,
                status=t.status,
                message=t.message,
                trace_url=t.trace_url or bucket.get('trace'),
                screenshot_url=t.screenshot_url or bucket.get('screenshot'),
            )
        )
    return tuple(out)


def build_end2end_failure(
    *,
    gate: str,
    log_tail: str,
    ui_artifacts: tuple[Artifact, ...] = (),
) -> End2EndFailure | None:
    """Top-level entry point — parse, classify, return a structured payload.

    Returns ``None`` when ``gate`` isn't an end2end gate name. The caller
    should filter checks with :func:`is_end2end_gate` first; this guard is a
    cheap defensive net so callers don't have to.

    ``log_tail`` is the raw step-log text (what the remote
    ``mcp__leartech-tekton__step_logs`` tool returns via
    ``${LEARTECH_MCP_URL}/mcp/tekton``). Parsing tolerates surrounding
    non-JSON content; see :func:`parse_results_json_from_log`.

    ``ui_artifacts`` is optional — when the caller has already parsed the
    parallel end2end-ui Playwright sticky (via
    :func:`gate.tools.playwright_artifacts.read_playwright_runs`), they can
    pass the artefact tuple here and we'll annotate the failed tests with
    matching screenshot / trace URLs.

    Returns a :class:`End2EndFailure` whose ``classification`` is one of
    ``real_failure`` / ``preview_infra`` and whose ``actionable`` flag is
    ``True`` iff the agent should iterate on this failure
    (``real_failure``). ``preview_infra`` failures are non-actionable: the
    agent should retest via chatops, not edit code.
    """
    if not is_end2end_gate(gate):
        return None
    results = parse_results_json_from_log(log_tail)
    classification = classify_end2end_failure(results, log_tail)
    if results is not None:
        summary = str(results.get('summary') or '').strip() or 'no summary in results.json'
        failed_tests_list = extract_failed_tests(results)
    else:
        summary = 'results.json not found in step log'
        failed_tests_list = []
    failed_tests = _annotate_failed_tests_with_artifacts(failed_tests_list, ui_artifacts)
    return End2EndFailure(
        gate=gate,
        classification=classification,
        summary=summary,
        failed_tests=failed_tests,
        actionable=(classification == 'real_failure'),
        artifact_urls=ui_artifacts if is_end2end_ui_gate(gate) else (),
    )


# ─── Orchestrator — fetches log via injected step_logs callable ──────────────


# Type alias for the tekton.step_logs callable so test seams stay narrow.
# Signature: (pipelinerun_name, step_name, cluster, tail) -> str.
StepLogsFn = Callable[[str, str, str, int], str]


def fetch_end2end_failure(
    *,
    gate: str,
    pipelinerun_name: str,
    cluster: str,
    step_logs_fn: StepLogsFn,
    step_name: str = 'run-tests',
    tail: int = 500,
    ui_artifacts: tuple[Artifact, ...] = (),
) -> End2EndFailure | None:
    """Fetch the failing step's log via the tekton MCP path and build a payload.

    This is the orchestrator the watcher calls when ``list_pr_checks``
    surfaces a failed ``end2end`` / ``end2end-ui`` check. ``step_logs_fn``
    is required — callers inject either the remote MCP-backed
    ``mcp__leartech-tekton__step_logs`` bridge or (in tests) a stub. There
    is no in-process default any more: the tekton MCP lives at
    ``${LEARTECH_MCP_URL}/mcp/tekton`` and callers reach it through the
    MCP layer, not by importing a local Python function.

    Soft-fail contract: if the ``step_logs_fn`` call raises any exception
    (transport error, context timeout, kubectl blip on the remote MCP),
    this function logs at WARN and returns ``None`` rather than letting
    the watcher crash. The watcher's next poll will re-attempt — by then
    the transient should have cleared. Mirrors the resilience pattern in
    :func:`gate.tools.playwright_artifacts.read_playwright_runs`.

    ``step_name`` defaults to ``run-tests`` (the canonical final step in
    the catalog's end2end task). Callers driving the orchestrator from a
    custom step name should pass it explicitly. Empty log →
    :func:`build_end2end_failure` handles it (returns a payload with
    ``results.json not found`` summary).
    """
    if not is_end2end_gate(gate):
        return None

    try:
        log_tail = step_logs_fn(pipelinerun_name, step_name, cluster, tail)
    except Exception as exc:  # noqa: BLE001 — soft-fail per contract above
        logger.warning(
            'end2end_gate: step_logs(%s, %s, %s) failed: %s; will retry on next poll',
            pipelinerun_name,
            step_name,
            cluster,
            exc,
        )
        return None

    return build_end2end_failure(gate=gate, log_tail=log_tail, ui_artifacts=ui_artifacts)


__all__ = [
    'End2EndFailure',
    'End2EndTest',
    'StepLogsFn',
    'build_end2end_failure',
    'classify_end2end_failure',
    'extract_failed_tests',
    'fetch_end2end_failure',
    'is_end2end_gate',
    'is_end2end_ui_gate',
    'parse_results_json_from_log',
]
