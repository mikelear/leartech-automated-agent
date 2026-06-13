"""govulncheck JSON output parser → list[Finding].

Go's ``govulncheck -json ./...`` emits a *stream* of JSON objects (one per
line, sometimes spread across many lines if pretty-printed). Each object
declares its kind via the top-level key — e.g. ``{"osv": {...}}``,
``{"finding": {...}}``, ``{"config": {...}}``.

We care about two kinds:

- ``osv`` — vulnerability metadata (GO-2024-… ID, summary, references).
- ``finding`` — a call-site reference to a vulnerable function. The
  ``finding.trace`` array's deepest entry is the user's code; shallower
  entries are the dependency chain. The presence (or absence) of a
  ``trace[0].position`` distinguishes a *called* vulnerability (severity
  high) from an *imported but not called* one (severity low).

Spec reference: https://pkg.go.dev/golang.org/x/vuln/internal/govulncheck
(the ``Message`` type is the wire shape).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gate.tools.parsers._common import (
    SEVERITY_HIGH,
    SEVERITY_LOW,
    Finding,
    normalise_severity,
)

logger = logging.getLogger(__name__)


def _iter_json_objects(content: str) -> list[dict[str, Any]]:
    """govulncheck output is a sequence of JSON objects.

    Tools that pipe through ``jq`` or pretty-print may wrap each object
    across multiple lines, so a strict line-by-line parse misses entries.
    We re-use the same balanced-brace walker the end2end parser uses,
    inlined here to avoid an import cycle.
    """
    out: list[dict[str, Any]] = []
    n = len(content)
    i = 0
    while i < n:
        if content[i] != '{':
            i += 1
            continue
        depth = 0
        start = i
        in_string = False
        escape = False
        while i < n:
            ch = content[i]
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
                        try:
                            obj = json.loads(content[start : i + 1])
                            if isinstance(obj, dict):
                                out.append(obj)
                        except json.JSONDecodeError:
                            pass
                        i += 1
                        break
            i += 1
        else:
            break
    return out


def _osv_summary(osv: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Extract ``(id, summary, references)`` from an OSV record.

    OSV record references can be a list of strings (older govulncheck) or
    a list of ``{type, url}`` dicts (newer). Coerce to plain URL strings
    either way.
    """
    osv_id = str(osv.get('id') or '<unknown>')
    summary = str(osv.get('summary') or osv.get('details') or '').strip()
    refs: list[str] = []
    raw_refs = osv.get('references')
    if isinstance(raw_refs, list):
        for ref in raw_refs[:3]:
            if isinstance(ref, str):
                refs.append(ref)
            elif isinstance(ref, dict) and ref.get('url'):
                refs.append(str(ref['url']))
    return osv_id, summary, refs


def parse_govulncheck_json(content: str | bytes) -> list[Finding]:
    """Parse govulncheck streaming JSON into list[Finding].

    Strategy:

    1. Walk the stream collecting OSV records (vuln metadata) into a
       lookup keyed by OSV ID.
    2. Walk findings; each finding cites an OSV by ID + optionally a
       deepest-trace position (= user code). If the trace has a position,
       the vuln is *called* → severity HIGH; otherwise it's *imported
       only* → severity LOW.
    3. Emit one Finding per (OSV-ID, deepest-position) tuple. Deduplicate
       — govulncheck can emit multiple call-site findings per vuln.

    Soft-fail on malformed input → ``[]``.
    """
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning('govulncheck_json: input is not valid UTF-8; returning empty findings')
            return []

    objects = _iter_json_objects(content)
    if not objects:
        return []

    osvs: dict[str, dict[str, Any]] = {}
    findings_raw: list[dict[str, Any]] = []
    for obj in objects:
        if 'osv' in obj and isinstance(obj['osv'], dict):
            osv = obj['osv']
            osv_id = str(osv.get('id') or '')
            if osv_id:
                osvs[osv_id] = osv
        if 'finding' in obj and isinstance(obj['finding'], dict):
            findings_raw.append(obj['finding'])

    # If no findings array was emitted (the modern format), some older
    # govulncheck versions ship a flat ``Vulns`` array. Handle that too.
    if not findings_raw:
        for obj in objects:
            vulns = obj.get('Vulns') or obj.get('vulns')
            if isinstance(vulns, list):
                for v in vulns:
                    if isinstance(v, dict):
                        # Coerce to finding-ish dict.
                        findings_raw.append({'osv': v.get('OSV', {}).get('id', ''), 'trace': []})

    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings_raw:
        osv_id = str(finding.get('osv') or '')
        if not osv_id:
            continue
        trace = finding.get('trace')
        deepest_position = ''
        called = False
        if isinstance(trace, list) and trace:
            first = trace[0]
            if isinstance(first, dict):
                pos = first.get('position')
                if isinstance(pos, dict):
                    filename = pos.get('filename', '')
                    line = pos.get('line', '')
                    if filename and line:
                        deepest_position = f'{filename}:{line}'
                        called = True
                func = first.get('function')
                if isinstance(func, dict) and func.get('name'):
                    deepest_position = deepest_position or str(func['name'])
        key = (osv_id, deepest_position)
        if key in seen:
            continue
        seen.add(key)
        osv = osvs.get(osv_id, {})
        _, summary, refs = _osv_summary(osv) if osv else (osv_id, '', [])
        severity = SEVERITY_HIGH if called else SEVERITY_LOW
        # Allow OSV-level severity (CVSS-style) to override the call-status default.
        osv_sev = ''
        for entry in (osv.get('severity') or []) if isinstance(osv, dict) else []:
            if isinstance(entry, dict) and entry.get('score'):
                # Heuristic: scores starting "9", "10", or containing "CRITICAL"
                # → critical; "7"/"8"/HIGH → high. Fall through otherwise.
                score = str(entry['score']).upper()
                if score.startswith(('CRITICAL', '9.', '10', '9 ')):
                    osv_sev = 'critical'
                    break
                if score.startswith(('HIGH', '7.', '8.')):
                    osv_sev = 'high'
                    break
        if osv_sev:
            severity = normalise_severity(osv_sev)
        extra: dict[str, Any] = {'called': called}
        if refs:
            extra['references'] = refs
        if not called:
            # Imported-only findings include both "no trace at all" and
            # "trace exists but points at a function name without a file
            # position" — neither shows the vuln being actually invoked.
            extra['imported_only'] = True
        out.append(
            Finding(
                severity=severity,
                rule=osv_id,
                location=deepest_position or '<imported>',
                message=summary or f'known vulnerability {osv_id}',
                extra=extra,
            )
        )
    return out


__all__ = ['parse_govulncheck_json']
