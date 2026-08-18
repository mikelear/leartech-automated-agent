"""JUnit XML test-result parser → list[Finding].

The de-facto standard for unit-test result XML, produced by pytest
(``--junitxml=...``), Go's gotestsum (``--junitfile``), Jest, mvn surefire,
JUnit itself, and a long tail of others. The catalog's unit-test gates
(``test``, ``lint``, ``pr``) and language-specific runners can all be
configured to emit JUnit XML.

Shape::

    <testsuites>
      <testsuite name="..." tests="N" failures="N" errors="N" skipped="N">
        <testcase name="..." classname="..." time="...">
          <failure type="..." message="...">stacktrace text</failure>
          <error type="..." message="...">stacktrace text</error>
          <skipped message="..."/>
        </testcase>
      </testsuite>
    </testsuites>

We parse:
- ``failure`` and ``error`` children of ``testcase`` → high-severity Finding
- ``skipped`` → info-severity Finding (NOT actionable, but tracked so the
  agent can see when a meant-to-run test was xfail'd / xskip'd)

We deliberately use stdlib ``xml.etree.ElementTree`` — adding ``lxml`` for
this would be overkill — and use a safe parser to avoid XXE / billion-laughs
attacks on adversarial input (per the standard library's documented
``defusedxml`` advisory; ``ElementTree`` parses without DTD expansion by
default since Python 3.7.1 but we add extra defence by stripping the
``DOCTYPE`` preamble if present).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from xml.etree.ElementTree import Element, ParseError  # noqa: S405

from defusedxml.ElementTree import fromstring

from gate.tools.parsers._common import Finding, normalise_severity

logger = logging.getLogger(__name__)


# Strip ``<!DOCTYPE ...>`` lines defensively. Python's ElementTree doesn't
# resolve external entities by default, but stripping the DOCTYPE removes
# the attack surface entirely. Pattern is permissive (DOTALL, lazy) but
# anchored to <!DOCTYPE so it can't eat legitimate XML.
_DOCTYPE_RE = re.compile(r'<!DOCTYPE[^>]*>', re.IGNORECASE)


def _strip_doctype(content: str) -> str:
    """Remove any DOCTYPE preamble — defence-in-depth against XXE."""
    return _DOCTYPE_RE.sub('', content)


def _safe_text(node: Element | None) -> str:
    """Return ``node.text or ''`` — convenience for missing-element cases."""
    if node is None or node.text is None:
        return ''
    return node.text.strip()


def _failure_finding(
    suite_name: str,
    classname: str,
    testname: str,
    failure_kind: str,
    elem: Element,
) -> Finding:
    """Build a Finding from a single ``<failure>``/``<error>``/``<skipped>`` element.

    ``failure_kind`` is one of ``failure``/``error``/``skipped``. The XML's
    ``type=`` attribute is the test framework's exception name; the
    ``message=`` attribute is its short summary; the element body is the
    stacktrace. We surface all three in a tidy ``message`` so the LLM can
    read it as a single block.
    """
    msg_attr = elem.attrib.get('message', '').strip()
    type_attr = elem.attrib.get('type', '').strip()
    body = (elem.text or '').strip()
    parts = [msg_attr] if msg_attr else []
    if type_attr and type_attr not in msg_attr:
        parts.append(f'({type_attr})')
    message = ' '.join(parts) or failure_kind
    extra: dict[str, Any] = {'failure_kind': failure_kind, 'suite': suite_name}
    if type_attr:
        extra['type'] = type_attr
    if body:
        # Keep the stacktrace separate so the prompt-render layer can render
        # it as a code block; truncate to keep the payload small (~200 lines).
        lines = body.splitlines()
        if len(lines) > 100:
            body = '\n'.join(lines[:50] + ['... (truncated) ...'] + lines[-50:])
        extra['stacktrace'] = body
    location = '::'.join(p for p in (classname, testname) if p) or testname
    return Finding(
        severity=normalise_severity(failure_kind),
        rule=type_attr or failure_kind,
        location=location,
        message=message,
        extra=extra,
    )


def parse_junit_xml(content: str | bytes) -> list[Finding]:
    """Parse JUnit XML content into a list of Findings.

    Soft-fail contract: malformed XML returns ``[]`` rather than raising.
    Handles three top-level envelopes:

    1. ``<testsuites>`` containing multiple ``<testsuite>`` (the canonical
       multi-suite shape pytest emits).
    2. A bare ``<testsuite>`` at the root (older Surefire / single-suite
       runners).
    3. A bare ``<testcase>`` at the root (extremely rare, but seen in
       fragments concatenated together).
    """
    if isinstance(content, bytes):
        try:
            content_str = content.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning('junit_xml: input is not valid UTF-8; returning empty findings')
            return []
    else:
        content_str = content

    content_str = _strip_doctype(content_str).strip()
    if not content_str:
        return []

    try:
        root = fromstring(content_str)
    except ParseError as exc:
        logger.warning('junit_xml: failed to parse XML (%s); returning empty findings', exc)
        return []

    # Normalise to a flat list of testsuite elements.
    suites: list[Element]
    tag = root.tag.lower()
    if tag == 'testsuites':
        suites = [s for s in root.findall('testsuite')]
    elif tag == 'testsuite':
        suites = [root]
    elif tag == 'testcase':
        # Synthesise a minimal suite so the loop below works uniformly.
        synthetic = Element('testsuite', {'name': ''})
        synthetic.append(root)
        suites = [synthetic]
    else:
        logger.warning('junit_xml: unexpected root tag %r; returning empty findings', root.tag)
        return []

    findings: list[Finding] = []
    for suite in suites:
        suite_name = suite.attrib.get('name', '')
        for case in suite.findall('testcase'):
            classname = case.attrib.get('classname', '')
            testname = case.attrib.get('name', '')
            for failure_kind in ('failure', 'error', 'skipped'):
                for elem in case.findall(failure_kind):
                    findings.append(_failure_finding(suite_name, classname, testname, failure_kind, elem))
    return findings


__all__ = ['parse_junit_xml']
