"""Structured artefact parsers for Tekton gate output (v6p0.6 step 1 of 4).

Re-exports the public surface so external callers can::

    from gate.tools.parsers import (
        ARTEFACT_PARSERS,
        Finding,
        GateFailure,
        parse_gate_artefact,
        parse_gate_artefact_auto,
        resolve_artefact_type,
    )

See individual modules for parser internals; see :mod:`registry` for the
dispatcher.
"""

from __future__ import annotations

from gate.tools.parsers._common import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
    Finding,
    GateFailure,
    normalise_severity,
    severity_rank,
)
from gate.tools.parsers.coverage_json import parse_coverage_json
from gate.tools.parsers.govulncheck_json import parse_govulncheck_json
from gate.tools.parsers.junit_xml import parse_junit_xml
from gate.tools.parsers.playwright_json import parse_playwright_json
from gate.tools.parsers.registry import (
    ARTEFACT_PARSERS,
    GATE_TO_ARTEFACT_TYPE,
    ParserFn,
    parse_gate_artefact,
    parse_gate_artefact_auto,
    resolve_artefact_type,
)
from gate.tools.parsers.results_json import parse_results_json
from gate.tools.parsers.sarif import parse_sarif
from gate.tools.parsers.trivy_json import parse_trivy_json

__all__ = [
    'ARTEFACT_PARSERS',
    'Finding',
    'GATE_TO_ARTEFACT_TYPE',
    'GateFailure',
    'ParserFn',
    'SEVERITY_CRITICAL',
    'SEVERITY_HIGH',
    'SEVERITY_INFO',
    'SEVERITY_LOW',
    'SEVERITY_MEDIUM',
    'SEVERITY_ORDER',
    'normalise_severity',
    'parse_coverage_json',
    'parse_gate_artefact',
    'parse_gate_artefact_auto',
    'parse_govulncheck_json',
    'parse_junit_xml',
    'parse_playwright_json',
    'parse_results_json',
    'parse_sarif',
    'parse_trivy_json',
    'resolve_artefact_type',
    'severity_rank',
]
