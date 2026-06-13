"""Playwright JSON reporter output parser → list[Finding].

``npx playwright test --reporter=json`` produces::

    {
      "config": {...},
      "suites": [
        {
          "title": "spec-1.spec.ts",
          "file": "spec-1.spec.ts",
          "specs": [
            {
              "title": "login form",
              "tests": [
                {
                  "results": [
                    {
                      "status": "failed" | "passed" | "timedOut" | "skipped",
                      "error": {"message": "...", "stack": "..."},
                      "attachments": [
                        {"name": "screenshot", "path": "..."},
                        {"name": "trace", "path": "..."},
                        {"name": "video", "path": "..."}
                      ]
                    }
                  ]
                }
              ]
            }
          ],
          "suites": [...]   // nested describe() blocks
        }
      ]
    }

Suites can nest (one ``describe()`` per level). We flatten depth-first so
findings carry the full breadcrumb in ``location``.

Extends the v6p0.5 :mod:`gate.tools.playwright_artifacts` work: that
module parses the *sticky comment* the gate posts (rendered markdown with
artefact URLs). This module parses the *raw JSON reporter output* — both
are useful, in different points of the pipeline.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gate.tools.parsers._common import Finding, normalise_severity

logger = logging.getLogger(__name__)


def _walk_suites(
    suites: list[Any],
    findings: list[Finding],
    breadcrumb: tuple[str, ...] = (),
) -> None:
    """Depth-first walk: for every spec/test/result, emit a Finding when failed."""
    for suite in suites:
        if not isinstance(suite, dict):
            continue
        title = str(suite.get('title') or suite.get('file') or '')
        next_breadcrumb = breadcrumb + (title,) if title else breadcrumb

        specs = suite.get('specs')
        if isinstance(specs, list):
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                spec_title = str(spec.get('title') or '')
                spec_file = str(spec.get('file') or '')
                tests = spec.get('tests')
                if not isinstance(tests, list):
                    continue
                for test in tests:
                    if not isinstance(test, dict):
                        continue
                    results = test.get('results')
                    if not isinstance(results, list):
                        continue
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        status = str(result.get('status') or '').lower()
                        if status not in ('failed', 'timedout', 'interrupted'):
                            continue
                        location_parts: list[str] = []
                        if spec_file:
                            location_parts.append(spec_file)
                        if next_breadcrumb:
                            location_parts.append(' > '.join(p for p in next_breadcrumb if p))
                        if spec_title:
                            location_parts.append(spec_title)
                        location = ' :: '.join(p for p in location_parts if p)
                        error = result.get('error') if isinstance(result.get('error'), dict) else None
                        message = ''
                        stack = ''
                        if isinstance(error, dict):
                            message = str(error.get('message') or '').strip()
                            stack = str(error.get('stack') or '').strip()
                        if not message:
                            message = f'{status} (no error message)'
                        extra: dict[str, Any] = {'status': status}
                        if stack:
                            lines = stack.splitlines()
                            if len(lines) > 80:
                                stack = '\n'.join(lines[:40] + ['... (truncated) ...'] + lines[-40:])
                            extra['stack'] = stack
                        attachments = result.get('attachments')
                        if isinstance(attachments, list):
                            for att in attachments:
                                if not isinstance(att, dict):
                                    continue
                                name = str(att.get('name') or '')
                                # Both 'path' (local) and 'url' (uploaded) keys appear;
                                # capture whichever is present.
                                target = att.get('url') or att.get('path')
                                if target and name in ('screenshot', 'trace', 'video'):
                                    extra.setdefault(f'{name}_urls', []).append(str(target))
                        retries = result.get('retry')
                        if isinstance(retries, int) and retries > 0:
                            extra['retry'] = retries
                        findings.append(
                            Finding(
                                severity=normalise_severity(status),
                                rule=status,
                                location=location or spec_title or '<unknown spec>',
                                message=message,
                                extra=extra,
                            )
                        )

        nested_suites = suite.get('suites')
        if isinstance(nested_suites, list):
            _walk_suites(nested_suites, findings, next_breadcrumb)


def parse_playwright_json(content: str | bytes) -> list[Finding]:
    """Parse Playwright JSON reporter output into list[Finding].

    Soft-fail: malformed JSON → ``[]``. Skipped / passed tests do NOT
    produce findings (no actionable signal). Failed / timedOut /
    interrupted tests produce one finding each, with the spec breadcrumb
    in ``location`` and any attachment URLs (screenshot, trace, video) in
    ``extra``.
    """
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning('playwright_json: input is not valid UTF-8; returning empty findings')
            return []

    try:
        doc = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning('playwright_json: input is not valid JSON (%s); returning empty findings', exc)
        return []

    if not isinstance(doc, dict):
        return []

    suites = doc.get('suites')
    if not isinstance(suites, list):
        return []

    findings: list[Finding] = []
    _walk_suites(suites, findings)
    return findings


__all__ = ['parse_playwright_json']
