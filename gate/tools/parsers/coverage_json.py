"""coverage.py JSON report parser → list[Finding].

``coverage json -o coverage.json`` produces::

    {
      "meta": {...},
      "totals": {
        "covered_lines": N, "num_statements": N,
        "percent_covered": 78.5, ...
      },
      "files": {
        "path/to/file.py": {
          "summary": {"percent_covered": 50.0, "missing_lines": [12, 13, 14], ...}
        },
        ...
      }
    }

Conversion strategy:

- One ``Finding`` for the overall total when ``percent_covered`` is below the
  configured ``threshold`` (default 80).
- One ``Finding`` per file whose ``percent_covered`` < ``threshold``. Severity
  scales with the gap: ≥30pp gap → high, ≥10pp → medium, ≥3pp → low,
  otherwise info (filtered out by the dispatcher).

Severity mapping matters because the agent uses ``GateFailure.actionable``
to decide whether to iterate — a single file 1pp below the threshold isn't
worth a respawn cycle.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gate.tools.parsers._common import (
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    Finding,
)

logger = logging.getLogger(__name__)


DEFAULT_THRESHOLD = 80.0


def _severity_for_gap(gap_pp: float) -> str:
    """Map a percentage-point gap below threshold to a canonical severity.

    The thresholds (30/10/3pp) are chosen empirically from PR-coverage data
    on the agent repo: ≥30pp gaps almost always indicate a wholly
    untested module (new code without tests); 10–30pp is "test exists but
    incomplete"; 3–10pp is the noise band where re-running with cached
    fixtures often closes the gap.
    """
    if gap_pp >= 30.0:
        return SEVERITY_HIGH
    if gap_pp >= 10.0:
        return SEVERITY_MEDIUM
    if gap_pp >= 3.0:
        return SEVERITY_LOW
    return SEVERITY_INFO


def parse_coverage_json(
    content: str | bytes,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Finding]:
    """Parse coverage.py JSON output into list[Finding].

    Returns no findings when overall + per-file coverage meet the threshold.
    Returns one finding for the total + one per below-threshold file when
    not — severity-tagged so the dispatcher can decide actionability.

    Soft-fail: malformed JSON / wrong shape → ``[]``.
    """
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning('coverage_json: input is not valid UTF-8; returning empty findings')
            return []

    try:
        doc = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning('coverage_json: input is not valid JSON (%s); returning empty findings', exc)
        return []

    if not isinstance(doc, dict):
        return []

    findings: list[Finding] = []
    totals = doc.get('totals')
    if isinstance(totals, dict):
        pct = totals.get('percent_covered')
        if isinstance(pct, (int, float)) and pct < threshold:
            gap = threshold - float(pct)
            findings.append(
                Finding(
                    severity=_severity_for_gap(gap),
                    rule='coverage_threshold',
                    location='<total>',
                    message=(f'Overall coverage {pct:.1f}% is below threshold {threshold:.1f}% (gap {gap:.1f}pp)'),
                    extra={
                        'percent_covered': float(pct),
                        'threshold': float(threshold),
                        'gap_pp': float(gap),
                    },
                )
            )

    files = doc.get('files')
    if isinstance(files, dict):
        # Sort by coverage ascending so worst offenders come first.
        per_file: list[tuple[str, float, list[int]]] = []
        for path, file_info in files.items():
            if not isinstance(file_info, dict):
                continue
            summary = file_info.get('summary')
            if not isinstance(summary, dict):
                continue
            pct = summary.get('percent_covered')
            if not isinstance(pct, (int, float)):
                continue
            if pct >= threshold:
                continue
            missing = summary.get('missing_lines')
            missing_list: list[int] = []
            if isinstance(missing, list):
                missing_list = [int(x) for x in missing if isinstance(x, int)]
            per_file.append((str(path), float(pct), missing_list))
        per_file.sort(key=lambda x: x[1])
        for path, pct, missing in per_file:
            gap = threshold - pct
            extra: dict[str, Any] = {
                'percent_covered': pct,
                'threshold': threshold,
                'gap_pp': gap,
            }
            if missing:
                # Cap missing-lines list so the finding doesn't blow up the
                # prompt token budget on a totally untested 1000-line file.
                if len(missing) > 30:
                    extra['missing_lines_sample'] = missing[:30]
                    extra['missing_lines_count'] = len(missing)
                else:
                    extra['missing_lines'] = missing
            findings.append(
                Finding(
                    severity=_severity_for_gap(gap),
                    rule='coverage_threshold',
                    location=path,
                    message=(f'{path}: {pct:.1f}% covered ({gap:.1f}pp below threshold {threshold:.1f}%)'),
                    extra=extra,
                )
            )
    return findings


__all__ = ['parse_coverage_json', 'DEFAULT_THRESHOLD']
