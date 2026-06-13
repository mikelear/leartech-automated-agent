"""Trivy native JSON parser → list[Finding].

Trivy's default JSON output (``trivy image --format json``, ``trivy fs
--format json``) is more compact than its SARIF output and is what most
existing leartech security-scan tasks already emit. The shape::

    {
      "SchemaVersion": 2,
      "ArtifactName": "...",
      "ArtifactType": "container_image" | "filesystem" | ...,
      "Results": [
        {
          "Target": "...",
          "Type": "alpine" | "python-pkg" | ...,
          "Vulnerabilities": [
            {
              "VulnerabilityID": "CVE-2024-1234",
              "PkgName": "...",
              "InstalledVersion": "...",
              "FixedVersion": "...",
              "Severity": "CRITICAL" | "HIGH" | ...,
              "Title": "...",
              "Description": "...",
              "References": [...]
            }
          ],
          "Misconfigurations": [
            {"ID": "...", "Severity": "...", "Title": "...", ...}
          ],
          "Secrets": [
            {"RuleID": "...", "Severity": "...", "Title": "...", ...}
          ]
        }
      ]
    }

We surface vulnerabilities, misconfigurations, AND secrets — Trivy treats
each as a separate result list, but for the agent they're all "things to
fix" and benefit from the unified Finding shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gate.tools.parsers._common import Finding, normalise_severity

logger = logging.getLogger(__name__)


def _build_vuln_finding(target: str, vuln: dict[str, Any]) -> Finding:
    """Vulnerability rows. ``CVE-...`` is the rule; package@version the location."""
    pkg = str(vuln.get('PkgName') or '<unknown-pkg>')
    installed = str(vuln.get('InstalledVersion') or '')
    fixed = str(vuln.get('FixedVersion') or '')
    location = f'{pkg}@{installed}' if installed else pkg
    title = str(vuln.get('Title') or '').strip()
    description = str(vuln.get('Description') or '').strip()
    message = title or description or vuln.get('VulnerabilityID', 'vulnerability')
    extra: dict[str, Any] = {'target': target}
    if fixed:
        extra['fixed_version'] = fixed
    if description and title and description != title:
        # Keep the description for prompt-rendering when distinct from title.
        # Truncate to 500 chars so a CVE with a verbose RHEL bulletin doesn't
        # blow the prompt budget.
        extra['description'] = description[:500]
    refs = vuln.get('References')
    if isinstance(refs, list) and refs:
        extra['references'] = refs[:3]  # cap; first refs are usually the canonical advisories
    return Finding(
        severity=normalise_severity(vuln.get('Severity')),
        rule=str(vuln.get('VulnerabilityID') or '<unknown>'),
        location=location,
        message=str(message),
        extra=extra,
    )


def _build_misconfig_finding(target: str, mc: dict[str, Any]) -> Finding:
    """Misconfiguration rows (IaC, Dockerfile, Kubernetes manifests)."""
    title = str(mc.get('Title') or '').strip()
    message = str(mc.get('Message') or '').strip()
    extra = {'target': target, 'kind': 'misconfiguration'}
    if mc.get('Resolution'):
        extra['resolution'] = mc['Resolution']
    return Finding(
        severity=normalise_severity(mc.get('Severity')),
        rule=str(mc.get('ID') or '<unknown>'),
        location=target,
        message=title or message or 'misconfiguration',
        extra=extra,
    )


def _build_secret_finding(target: str, secret: dict[str, Any]) -> Finding:
    """Secret-leak rows. Trivy reports the line + a (redacted) match."""
    rule_id = str(secret.get('RuleID') or '<unknown>')
    title = str(secret.get('Title') or '').strip()
    line = secret.get('StartLine')
    location = f'{target}:{line}' if isinstance(line, int) else target
    extra: dict[str, Any] = {'target': target, 'kind': 'secret'}
    if secret.get('Match'):
        # Trivy redacts the match content itself, but the surrounding context
        # is small and useful for the agent.
        extra['match'] = secret['Match']
    return Finding(
        severity=normalise_severity(secret.get('Severity')),
        rule=rule_id,
        location=location,
        message=title or 'leaked secret detected',
        extra=extra,
    )


def parse_trivy_json(content: str | bytes) -> list[Finding]:
    """Parse Trivy native JSON output into list[Finding].

    Returns ``[]`` on malformed input. When Trivy reports no findings (e.g.
    a clean scan) ``Results`` may be empty or omitted entirely — that's
    "no findings" rather than a parse failure, and we return ``[]``
    accordingly.

    Each ``Vulnerability`` / ``Misconfiguration`` / ``Secret`` becomes its
    own ``Finding`` so the dispatcher can render them uniformly.
    """
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning('trivy_json: input is not valid UTF-8; returning empty findings')
            return []

    try:
        doc = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning('trivy_json: input is not valid JSON (%s); returning empty findings', exc)
        return []

    if not isinstance(doc, dict):
        return []

    findings: list[Finding] = []
    results = doc.get('Results')
    if not isinstance(results, list):
        return findings
    for result in results:
        if not isinstance(result, dict):
            continue
        target = str(result.get('Target') or '<unknown>')
        for vuln in result.get('Vulnerabilities') or []:
            if isinstance(vuln, dict):
                findings.append(_build_vuln_finding(target, vuln))
        for mc in result.get('Misconfigurations') or []:
            if isinstance(mc, dict):
                findings.append(_build_misconfig_finding(target, mc))
        for secret in result.get('Secrets') or []:
            if isinstance(secret, dict):
                findings.append(_build_secret_finding(target, secret))
    return findings


__all__ = ['parse_trivy_json']
