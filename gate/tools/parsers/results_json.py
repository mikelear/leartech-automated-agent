"""Thin wrapper around v6p0.5's ``parse_results_json_from_log`` → list[Finding].

The end2end harness's ``results.json`` payload was already parsed by
:mod:`gate.tools.end2end_gate` in v6p0.5. This module re-uses that parser
and converts its output into the canonical ``Finding`` shape so end2end
gates participate in the same ``GateFailure`` envelope as every other
artefact type.

Why a wrapper rather than direct re-export
------------------------------------------

The v6p0.5 :class:`gate.tools.end2end_gate.End2EndFailure` is the right
shape for end2end-specific consumers (it carries Playwright artefact URLs,
preview-infra vs real-failure classification, etc.). The general
``GateFailure`` payload is the lowest common denominator across all
artefact types. Both must keep working:

- end2end-specific code paths (iteration loop's preview-infra detection)
  continue to use ``End2EndFailure``.
- Generalised code paths (the new v6p0.6 watcher dispatch) use
  ``GateFailure`` so they don't need a per-gate switch.

This module is the bridge — it takes raw log/results.json content and
returns ``list[Finding]`` for the generalised path. The end2end-specific
classification stays in ``end2end_gate.py`` untouched.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gate.tools.end2end_gate import parse_results_json_from_log
from gate.tools.parsers._common import Finding, normalise_severity

logger = logging.getLogger(__name__)


def parse_results_json(content: str | bytes) -> list[Finding]:
    """Parse an end2end harness results.json document into Findings.

    Accepts either:
    - a raw ``results.json`` document, OR
    - a step-log tail containing a ``results.json`` block somewhere inside
      (we delegate extraction to :func:`parse_results_json_from_log`).

    Soft-fail contract: returns ``[]`` on malformed input, missing
    ``tests`` array, or pass-only test rows (nothing to report).
    """
    if isinstance(content, bytes):
        try:
            content_str = content.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning('results_json: input is not valid UTF-8; returning empty findings')
            return []
    else:
        content_str = content

    # Try direct JSON parse first — the artefact may be a pure results.json
    # dump rather than a log tail.
    doc: dict[str, Any] | None = None
    stripped = content_str.strip()
    if stripped.startswith('{'):
        try:
            candidate = json.loads(stripped)
            if isinstance(candidate, dict) and 'tests' in candidate:
                doc = candidate
        except json.JSONDecodeError:
            doc = None

    if doc is None:
        doc = parse_results_json_from_log(content_str)

    if doc is None:
        return []

    tests = doc.get('tests')
    if not isinstance(tests, list):
        return []

    findings: list[Finding] = []
    for row in tests:
        if not isinstance(row, dict):
            continue
        status = str(row.get('status', '') or '').lower()
        if status not in ('fail', 'failed', 'error', 'skipped'):
            continue
        name = str(row.get('name', '') or '<unknown>')
        message = str(row.get('message', '') or '').strip()
        extra: dict[str, Any] = {}
        for key in ('trace_url', 'screenshot_url', 'video_url'):
            val = row.get(key)
            if val:
                extra[key] = val
        findings.append(
            Finding(
                severity=normalise_severity(status),
                rule=status,
                location=name,
                message=message or f'test {name} reported status={status}',
                extra=extra,
            )
        )
    return findings


__all__ = ['parse_results_json']
