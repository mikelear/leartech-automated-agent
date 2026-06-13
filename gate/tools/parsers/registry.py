"""ARTEFACT_PARSERS registry + dispatcher.

This module is the public seam the watcher's iteration loop calls. It
wraps the per-type parsers (each a pure function over raw artefact
content) with:

1. A mapping ``artefact_type -> parser`` (the ``ARTEFACT_PARSERS`` dict).
2. A best-effort ``gate_name -> artefact_type`` resolution (the
   ``GATE_TO_ARTEFACT_TYPE`` mapping).
3. A top-level :func:`parse_gate_artefact` that ties them together,
   building a :class:`GateFailure` envelope.

Per the v6p0.6 step-1 plan, this module **does not** know how to actually
*fetch* an artefact from the cluster — that's the orchestrator's job (a
Tekton MCP / GCS / PVC mount concern that varies by deployment topology).
Callers pass in the already-fetched content; we just dispatch + parse.

When the artefact-fetch layer lands (step 2 of the v6p0.6 plan), it will
plug into this module via a small adapter — see
:func:`parse_gate_artefact` for the integration point.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from gate.tools.parsers._common import Finding, GateFailure
from gate.tools.parsers.coverage_json import parse_coverage_json
from gate.tools.parsers.govulncheck_json import parse_govulncheck_json
from gate.tools.parsers.junit_xml import parse_junit_xml
from gate.tools.parsers.playwright_json import parse_playwright_json
from gate.tools.parsers.results_json import parse_results_json
from gate.tools.parsers.sarif import parse_sarif
from gate.tools.parsers.trivy_json import parse_trivy_json

logger = logging.getLogger(__name__)


# ─── Parser registry ─────────────────────────────────────────────────────────

#: Signature shared by every artefact parser. Parsers accept ``str | bytes``
#: so callers can pass the raw content from kubectl logs (str) or from a
#: PVC / GCS download (bytes) without an upfront decode step.
ParserFn = Callable[[str | bytes], list[Finding]]


#: Authoritative artefact-type → parser map. Keys are the canonical
#: artefact-type tokens the watcher dispatcher uses; values are the pure
#: parser functions imported above. Adding a new artefact type means:
#:
#: 1. Add the parser module under ``gate/tools/parsers/<name>.py``.
#: 2. Add an entry here.
#: 3. (Optionally) add a gate → type mapping in ``GATE_TO_ARTEFACT_TYPE``.
#:
#: No other code needs to change — the dispatcher discovers types via this
#: dict, and the GateFailure envelope is shape-agnostic.
ARTEFACT_PARSERS: Final[dict[str, ParserFn]] = {
    'sarif': parse_sarif,
    'junit': parse_junit_xml,
    'results_json': parse_results_json,
    'coverage_json': parse_coverage_json,
    'trivy_json': parse_trivy_json,
    'govulncheck_json': parse_govulncheck_json,
    'playwright_json': parse_playwright_json,
}


# ─── Gate → artefact-type resolution ─────────────────────────────────────────


_CLUSTER_PREFIXES: Final = ('gcp/', 'az/')


def _strip_cluster_prefix(gate_name: str) -> str:
    """Drop a leading ``gcp/`` or ``az/`` so callers can key on the
    cluster-agnostic name. Mirrors the existing pattern in
    :mod:`gate.tools.end2end_gate`."""
    for prefix in _CLUSTER_PREFIXES:
        if gate_name.startswith(prefix):
            return gate_name[len(prefix) :]
    return gate_name


#: Best-effort mapping from a stripped gate name to its canonical artefact
#: type. The values are ARTEFACT_PARSERS keys.
#:
#: Multiple gates can map to the same artefact type — that's expected.
#: When a gate emits multiple artefact types (e.g. security-scan can emit
#: both SARIF *and* native Trivy JSON), we list the primary one here; the
#: orchestrator can call :func:`parse_gate_artefact` once per artefact.
#:
#: Gates not listed fall back to ``None`` — the dispatcher then uses the
#: existing step-log heuristic dispatcher
#: (:mod:`gate.agent.step_failure_diagnosis`) as the safety net.
GATE_TO_ARTEFACT_TYPE: Final[dict[str, str]] = {
    'security-scan': 'sarif',
    'image-scan': 'sarif',
    'dynamic-scan': 'sarif',
    'test': 'junit',
    'end2end': 'results_json',
    'end2end-ui': 'playwright_json',
    # ``coverage`` and ``pr`` can carry coverage.json under specific
    # repo conventions; mapped explicitly so opt-in repos light up. Others
    # fall through to the heuristic dispatcher.
    'coverage': 'coverage_json',
}


def resolve_artefact_type(gate_name: str) -> str | None:
    """Return the canonical artefact type for ``gate_name``, or ``None``.

    Accepts both the bare gate name (``end2end``) and a cluster-prefixed
    one (``gcp/end2end``). Returns ``None`` when no mapping exists — the
    caller should then fall through to the heuristic dispatcher.
    """
    bare = _strip_cluster_prefix(gate_name)
    return GATE_TO_ARTEFACT_TYPE.get(bare)


# ─── Dispatcher ──────────────────────────────────────────────────────────────


def parse_gate_artefact(
    *,
    gate: str,
    artefact_type: str | None,
    content: str | bytes,
    raw_log_tail: str = '',
) -> GateFailure | None:
    """Top-level entry point for the watcher.

    Resolves the parser, runs it, and wraps the result in a
    :class:`GateFailure`. Returns ``None`` when:

    - ``artefact_type`` is ``None`` (caller should fall back to the
      heuristic step-log dispatcher), OR
    - ``artefact_type`` is unrecognised (logged at WARN), OR
    - parsing raises despite the per-parser soft-fail contract (defensive
      outer try; logged at WARN).

    Returns a populated ``GateFailure`` when the parser succeeds —
    *including* when the findings list is empty (e.g. a clean SARIF run).
    Empty-findings is a real signal: it means the artefact was readable
    and contained no actionable issues. Callers distinguish it from
    "parsing failed" by the non-None return.

    ``raw_log_tail`` is stored on the GateFailure so the prompt-render
    layer can fall through to log-tail rendering when ``findings`` is empty
    but the gate is still RED.
    """
    if artefact_type is None:
        return None

    parser = ARTEFACT_PARSERS.get(artefact_type)
    if parser is None:
        logger.warning(
            'parse_gate_artefact: unknown artefact_type %r for gate %s; falling back to heuristic log-tail dispatch',
            artefact_type,
            gate,
        )
        return None

    try:
        findings = parser(content)
    except Exception as exc:  # noqa: BLE001 — defensive belt-and-braces
        logger.warning(
            'parse_gate_artefact: parser %s raised on gate %s (%s); returning empty findings + log-tail fallback',
            artefact_type,
            gate,
            exc,
        )
        findings = []

    return GateFailure(
        gate=gate,
        artefact_type=artefact_type,
        findings=tuple(findings),
        raw_log_tail=raw_log_tail,
    )


def parse_gate_artefact_auto(
    *,
    gate: str,
    content: str | bytes,
    raw_log_tail: str = '',
) -> GateFailure | None:
    """Convenience: resolve artefact_type from gate name, then dispatch.

    Returns ``None`` when no gate→type mapping exists — callers must fall
    back to the heuristic dispatcher.
    """
    artefact_type = resolve_artefact_type(gate)
    if artefact_type is None:
        return None
    return parse_gate_artefact(
        gate=gate,
        artefact_type=artefact_type,
        content=content,
        raw_log_tail=raw_log_tail,
    )


__all__ = [
    'ARTEFACT_PARSERS',
    'GATE_TO_ARTEFACT_TYPE',
    'ParserFn',
    'parse_gate_artefact',
    'parse_gate_artefact_auto',
    'resolve_artefact_type',
]
