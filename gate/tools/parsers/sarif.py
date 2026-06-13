"""SARIF 2.1.0 parser → list[Finding].

The Static Analysis Results Interchange Format is the canonical structured
output for security scanners (Trivy's ``--format sarif``, Grype's ``-o sarif``,
CodeQL, Bandit, Semgrep, …). The catalog's security-scan / image-scan /
dynamic-scan gates all emit SARIF, so a single parser covers them all.

Spec reference: https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html

Shape this parser cares about (heavily abbreviated)::

    {
      "version": "2.1.0",
      "runs": [
        {
          "tool": {"driver": {"name": "trivy", "rules": [...]}},
          "results": [
            {
              "ruleId": "CVE-2024-1234",
              "level": "error" | "warning" | "note" | "none",
              "message": {"text": "..."},
              "locations": [{
                "physicalLocation": {
                  "artifactLocation": {"uri": "path/to/file"},
                  "region": {"startLine": 12}
                }
              }]
            }
          ]
        }
      ]
    }

We tolerate older-spec quirks (e.g. ``message`` as a bare string, missing
``locations`` array) per SARIF 2.0 transition guidance — many real tools
emit slightly off-spec output.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gate.tools.parsers._common import Finding, normalise_severity

logger = logging.getLogger(__name__)


def _coerce_message(raw: Any) -> str:
    """SARIF ``message`` is normatively ``{"text": ...}`` but tools sometimes
    emit a bare string or include ``markdown``-only variants. Coerce to plain
    text without raising."""
    if not raw:
        return ''
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        # Prefer text; fall back to markdown; finally stringify the whole dict.
        for key in ('text', 'markdown'):
            val = raw.get(key)
            if isinstance(val, str) and val:
                return val
        return str(raw)
    return str(raw)


def _coerce_location(locations: Any) -> str:
    """Return the first physical location as ``uri:line``, or empty string.

    SARIF allows ``locations`` to be absent (rule-level findings without a
    file anchor) — in that case we return the empty string and the dispatcher
    surfaces the finding without a location prefix.
    """
    if not isinstance(locations, list) or not locations:
        return ''
    first = locations[0]
    if not isinstance(first, dict):
        return ''
    physical = first.get('physicalLocation')
    if not isinstance(physical, dict):
        return ''
    uri = ''
    artifact = physical.get('artifactLocation')
    if isinstance(artifact, dict):
        uri = str(artifact.get('uri') or '')
    region = physical.get('region')
    start_line = ''
    if isinstance(region, dict):
        line = region.get('startLine')
        if isinstance(line, int):
            start_line = str(line)
    if uri and start_line:
        return f'{uri}:{start_line}'
    return uri


def _resolve_rule_severity(
    result: dict[str, Any],
    rules_by_id: dict[str, dict[str, Any]],
) -> str:
    """SARIF severity has two layers: ``result.level`` (warning/error/note/none)
    and the more granular ``result.rank``/``properties.security-severity``
    that Trivy and friends attach.

    Decision order:
    1. ``properties['security-severity']`` (Trivy attaches "9.8"-style CVSS)
       → critical >= 9, high >= 7, medium >= 4, low > 0.
    2. ``rule.defaultConfiguration.level`` from the tool's rule definition.
    3. ``result.level``.
    4. Default: ``warning`` → medium.
    """
    props = result.get('properties') if isinstance(result.get('properties'), dict) else {}
    severity_score = props.get('security-severity') if isinstance(props, dict) else None
    if severity_score is not None:
        try:
            score = float(severity_score)
        except (TypeError, ValueError):
            score = -1.0
        if score >= 9.0:
            return 'critical'
        if score >= 7.0:
            return 'high'
        if score >= 4.0:
            return 'medium'
        if score > 0:
            return 'low'

    # Fall back to result.level / rule.defaultConfiguration.level
    level = result.get('level')
    if not level:
        rule_id = result.get('ruleId')
        if rule_id and rule_id in rules_by_id:
            rule_def = rules_by_id[rule_id]
            default_config = rule_def.get('defaultConfiguration')
            if isinstance(default_config, dict):
                level = default_config.get('level')
    return normalise_severity(level or 'warning')


def parse_sarif(content: str | bytes) -> list[Finding]:
    """Parse SARIF JSON content into a list of Findings.

    Soft-fail contract: malformed JSON, missing required keys, or
    spec-deviating shapes return an empty list rather than raising. The
    dispatcher (:func:`gate.tools.parsers.parse_gate_artefact`) treats
    empty-findings as "fall through to log tail", which is the right
    fallback shape.

    Handles both the standard envelope (``{"runs": [...]}``) and the rare
    single-run shape some tools emit. Multiple runs are flattened — each
    run's results contribute to the returned list, with tool name
    preserved in ``extra['tool']``.
    """
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning('sarif: input is not valid UTF-8; returning empty findings')
            return []

    try:
        doc = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning('sarif: input is not valid JSON (%s); returning empty findings', exc)
        return []

    if not isinstance(doc, dict):
        logger.warning('sarif: top-level is not an object; returning empty findings')
        return []

    runs = doc.get('runs')
    if not isinstance(runs, list):
        logger.warning('sarif: no "runs" array; returning empty findings')
        return []

    findings: list[Finding] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        tool_name = ''
        tool = run.get('tool')
        rules_by_id: dict[str, dict[str, Any]] = {}
        if isinstance(tool, dict):
            driver = tool.get('driver')
            if isinstance(driver, dict):
                tool_name = str(driver.get('name') or '')
                # Build a rule-id → rule dict map so we can pick up default
                # severities (some scanners emit ``level`` only at the rule
                # level, not on each result).
                rules = driver.get('rules')
                if isinstance(rules, list):
                    for rule in rules:
                        if isinstance(rule, dict) and rule.get('id'):
                            rules_by_id[str(rule['id'])] = rule
        results = run.get('results')
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            rule_id = str(result.get('ruleId') or '<unknown-rule>')
            severity = _resolve_rule_severity(result, rules_by_id)
            message = _coerce_message(result.get('message'))
            location = _coerce_location(result.get('locations'))
            extra: dict[str, Any] = {}
            if tool_name:
                extra['tool'] = tool_name
            # Preserve raw level + score for downstream rendering when present.
            if result.get('level'):
                extra['raw_level'] = result['level']
            props = result.get('properties')
            if isinstance(props, dict) and props.get('security-severity'):
                extra['security_severity_score'] = props['security-severity']
            findings.append(
                Finding(
                    severity=severity,
                    rule=rule_id,
                    location=location,
                    message=message,
                    extra=extra,
                )
            )
    return findings


__all__ = ['parse_sarif']
