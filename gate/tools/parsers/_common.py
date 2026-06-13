"""Shared dataclasses + severity normalisation for structured artefact parsers.

The v6p0.6-step-1 plan generalises the v6p0.5 ``end2end_failure`` payload
(`gate.tools.end2end_gate.End2EndFailure`) across every gate that produces
a known artefact type — SARIF for security/image scans, JUnit XML for unit
tests, results.json for end2end, coverage.json, Trivy native JSON,
govulncheck JSON, and Playwright JSON.

The common payload contract (per the initiative goal) is::

    {
      "kind": "gate_failure",
      "gate": "az/security-scan",
      "artefact_type": "sarif",
      "findings": [
        {"severity": "critical", "rule": "...", "location": "...", "message": "..."}
      ],
      "raw_log_tail": "..." (fallback when artefact absent)
    }

Each parser returns ``list[Finding]`` so the dispatcher can wrap them into a
:class:`GateFailure` with the right ``gate`` + ``artefact_type``. Parsers do
NOT know which gate produced the input — that's the dispatcher's job. Keeping
parsers gate-agnostic means a single SARIF parser handles security-scan,
image-scan, AND any future SARIF-emitting tool without per-gate special
cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

# ─── Severity normalisation ──────────────────────────────────────────────────


#: Canonical severity tokens, in descending importance order.
#:
#: We standardise on this lowercase 5-bucket vocabulary across every parser
#: even though the source artefacts use different naming (SARIF uses
#: error/warning/note/none; Trivy uses CRITICAL/HIGH/MEDIUM/LOW/UNKNOWN;
#: pytest/JUnit uses error/failure/skipped). The agent's iteration logic
#: dispatches on severity, so a stable vocabulary is essential — otherwise
#: every consumer would have to know each tool's native naming.
SEVERITY_CRITICAL: Final = 'critical'
SEVERITY_HIGH: Final = 'high'
SEVERITY_MEDIUM: Final = 'medium'
SEVERITY_LOW: Final = 'low'
SEVERITY_INFO: Final = 'info'

SEVERITY_ORDER: Final[tuple[str, ...]] = (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_LOW,
    SEVERITY_INFO,
)


# Mapping table from tool-native tokens (case-insensitive) to our canonical
# severities. New tools just need an entry here — no parser code changes.
_SEVERITY_ALIASES: Final[dict[str, str]] = {
    # Trivy / Grype / sysdig-style
    'critical': SEVERITY_CRITICAL,
    'high': SEVERITY_HIGH,
    'medium': SEVERITY_MEDIUM,
    'moderate': SEVERITY_MEDIUM,
    'low': SEVERITY_LOW,
    'negligible': SEVERITY_LOW,
    'info': SEVERITY_INFO,
    'informational': SEVERITY_INFO,
    'unknown': SEVERITY_INFO,
    # SARIF: error/warning/note/none — fold 'note' + 'none' to info so a
    # benign SARIF run doesn't pollute the agent's "fix this" bucket.
    'error': SEVERITY_HIGH,
    'warning': SEVERITY_MEDIUM,
    'note': SEVERITY_INFO,
    'none': SEVERITY_INFO,
    # JUnit: error/failure are both blocking; skipped is info.
    'failure': SEVERITY_HIGH,
    'failed': SEVERITY_HIGH,
    'fail': SEVERITY_HIGH,  # end2end harness's short form
    'skipped': SEVERITY_INFO,
    'skip': SEVERITY_INFO,
    # Playwright: failed/timedOut/passed; passed shouldn't reach us but be defensive.
    'timedout': SEVERITY_HIGH,
    'passed': SEVERITY_INFO,
    'interrupted': SEVERITY_HIGH,
    # govulncheck: CALLED (known-vulnerable call path) is high; IMPORTED is low.
    'called': SEVERITY_HIGH,
    'imported': SEVERITY_LOW,
}


def normalise_severity(raw: str | None) -> str:
    """Fold a tool-native severity token into our canonical 5-bucket vocabulary.

    Unknown / empty input returns ``info`` rather than raising, because a
    soft-fail keeps the parser working when an upstream tool ships a new
    severity name. The agent's downstream filter (which usually drops
    ``info`` + ``low``) then naturally ignores anything we couldn't classify.
    """
    if not raw:
        return SEVERITY_INFO
    return _SEVERITY_ALIASES.get(str(raw).strip().lower(), SEVERITY_INFO)


def severity_rank(severity: str) -> int:
    """Return a sort key — lower is more severe.

    Useful for ordering ``findings`` by severity (critical → info) without
    callers having to remember the ordering.
    """
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)  # unknown sinks below info


# ─── Finding + GateFailure dataclasses ───────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One structured row from a parsed artefact.

    Shape is intentionally minimal so the same dict survives a JSON
    round-trip through the agent's ``feedback_payloads`` (where the LLM
    reads it as a prompt block) without needing a custom encoder. Tool-
    specific extras (CVE ID, package name, JUnit stacktrace, etc.) live in
    :attr:`extra` so the LLM can surface them when relevant but downstream
    code can ignore them safely.
    """

    severity: str  # one of the SEVERITY_* constants
    rule: str  # rule identifier (e.g. 'CVE-2024-1234', 'B608', 'GO-2024-1234')
    location: str  # 'path/to/file.py:line' or '<test-class>::<test-name>' or 'package@version'
    message: str  # human-readable description
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'severity': self.severity,
            'rule': self.rule,
            'location': self.location,
            'message': self.message,
        }
        if self.extra:
            # Copy so the caller can't mutate our frozen instance via the dict.
            out['extra'] = dict(self.extra)
        return out


@dataclass(frozen=True)
class GateFailure:
    """Structured payload describing a failed gate.

    Generalises :class:`gate.tools.end2end_gate.End2EndFailure` across every
    gate that emits a recognised artefact type. See module docstring for
    the contract.

    ``raw_log_tail`` is preserved on the dataclass so the agent (or the
    eventual sticky-comment writer) can fall through to log-tail rendering
    when ``findings`` is empty — e.g. when the artefact path was misconfigured
    or the gate crashed before emitting structured output.
    """

    gate: str  # full check name, cluster-prefixed (e.g. 'az/security-scan')
    artefact_type: str  # one of the ARTEFACT_PARSERS keys
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    raw_log_tail: str = ''

    @property
    def actionable(self) -> bool:
        """True iff any finding is severe enough to merit an iteration.

        Severity threshold matches what most consumer-gate scoring uses
        (critical/high/medium). ``low`` and ``info`` are treated as
        non-blocking — the agent should surface them but not respawn.
        """
        threshold = severity_rank(SEVERITY_MEDIUM)
        return any(severity_rank(f.severity) <= threshold for f in self.findings)

    @property
    def top_severity(self) -> str:
        """The most severe finding's severity, or ``info`` when empty."""
        if not self.findings:
            return SEVERITY_INFO
        return min(self.findings, key=lambda f: severity_rank(f.severity)).severity

    def to_dict(self) -> dict[str, Any]:
        """Render the JSON-serialisable contract payload.

        Findings are emitted in severity-descending order so a human reading
        the rendered prompt block sees the worst issues first.
        """
        sorted_findings = sorted(self.findings, key=lambda f: severity_rank(f.severity))
        out: dict[str, Any] = {
            'kind': 'gate_failure',
            'gate': self.gate,
            'artefact_type': self.artefact_type,
            'findings': [f.to_dict() for f in sorted_findings],
            'actionable': self.actionable,
            'top_severity': self.top_severity,
        }
        if self.raw_log_tail:
            out['raw_log_tail'] = self.raw_log_tail
        return out


__all__ = [
    'Finding',
    'GateFailure',
    'SEVERITY_CRITICAL',
    'SEVERITY_HIGH',
    'SEVERITY_INFO',
    'SEVERITY_LOW',
    'SEVERITY_MEDIUM',
    'SEVERITY_ORDER',
    'normalise_severity',
    'severity_rank',
]
