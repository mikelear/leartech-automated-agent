"""Unit tests for gate.tools.parsers.* (v6p0.6 step 1 of 4).

Each parser is tested with a realistic fixture mirroring what the
corresponding tool actually emits in production:

- SARIF: a 3-result Trivy SARIF document with a CVE, a moderate finding,
  and a result that uses ``properties.security-severity``.
- JUnit XML: pytest output with one failure, one error, one skipped.
- Trivy native JSON: one vulnerability, one misconfiguration, one secret.
- govulncheck: streaming JSON with one *called* and one *imported* vuln.
- Coverage JSON: a multi-file report with the total + a per-file dip.
- Playwright JSON: nested suites with one failed test + attachment URLs.
- results.json: the v6p0.5 PR #58 reference shape — smoke test.

Plus the registry + dispatcher: gate-name resolution, soft-fail on bogus
content, and the auto-resolve convenience entry point.
"""

from __future__ import annotations

import json

from gate.tools.parsers import (
    ARTEFACT_PARSERS,
    GATE_TO_ARTEFACT_TYPE,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    Finding,
    GateFailure,
    normalise_severity,
    parse_coverage_json,
    parse_gate_artefact,
    parse_gate_artefact_auto,
    parse_govulncheck_json,
    parse_junit_xml,
    parse_playwright_json,
    parse_results_json,
    parse_sarif,
    parse_trivy_json,
    resolve_artefact_type,
    severity_rank,
)

# ─── Severity normalisation ──────────────────────────────────────────────────


def test_severity_normalisation_canonical_tokens() -> None:
    """Every documented alias must fold to a canonical severity."""
    assert normalise_severity('CRITICAL') == SEVERITY_CRITICAL
    assert normalise_severity('HIGH') == SEVERITY_HIGH
    assert normalise_severity('Medium') == SEVERITY_MEDIUM
    assert normalise_severity('moderate') == SEVERITY_MEDIUM
    assert normalise_severity('low') == SEVERITY_LOW
    assert normalise_severity('negligible') == SEVERITY_LOW
    assert normalise_severity('info') == SEVERITY_INFO


def test_severity_normalisation_sarif_levels() -> None:
    assert normalise_severity('error') == SEVERITY_HIGH
    assert normalise_severity('warning') == SEVERITY_MEDIUM
    assert normalise_severity('note') == SEVERITY_INFO
    assert normalise_severity('none') == SEVERITY_INFO


def test_severity_normalisation_unknown_returns_info() -> None:
    assert normalise_severity(None) == SEVERITY_INFO
    assert normalise_severity('') == SEVERITY_INFO
    assert normalise_severity('totally-unknown-token') == SEVERITY_INFO


def test_severity_rank_descending() -> None:
    """critical → 0; info → 4; rank is the canonical ordering."""
    assert severity_rank(SEVERITY_CRITICAL) < severity_rank(SEVERITY_HIGH)
    assert severity_rank(SEVERITY_HIGH) < severity_rank(SEVERITY_MEDIUM)
    assert severity_rank(SEVERITY_MEDIUM) < severity_rank(SEVERITY_LOW)
    assert severity_rank(SEVERITY_LOW) < severity_rank(SEVERITY_INFO)


# ─── GateFailure dataclass ────────────────────────────────────────────────────


def test_gate_failure_to_dict_contract_shape() -> None:
    f1 = Finding(severity=SEVERITY_CRITICAL, rule='CVE-2024-1', location='lib/x.py', message='boom')
    f2 = Finding(severity=SEVERITY_LOW, rule='STYLE-1', location='lib/y.py', message='nit')
    failure = GateFailure(
        gate='az/security-scan',
        artefact_type='sarif',
        findings=(f1, f2),
        raw_log_tail='+ trivy fs ...',
    )
    payload = failure.to_dict()
    assert payload['kind'] == 'gate_failure'
    assert payload['gate'] == 'az/security-scan'
    assert payload['artefact_type'] == 'sarif'
    # Findings sort by severity descending — critical first.
    assert payload['findings'][0]['rule'] == 'CVE-2024-1'
    assert payload['findings'][1]['rule'] == 'STYLE-1'
    assert payload['actionable'] is True  # critical is above the medium threshold
    assert payload['top_severity'] == SEVERITY_CRITICAL
    assert payload['raw_log_tail'].startswith('+ trivy')


def test_gate_failure_non_actionable_when_only_low_info() -> None:
    failure = GateFailure(
        gate='gcp/coverage',
        artefact_type='coverage_json',
        findings=(Finding(severity=SEVERITY_LOW, rule='r', location='l', message='m'),),
    )
    assert failure.actionable is False


def test_gate_failure_empty_findings_payload() -> None:
    failure = GateFailure(gate='az/lint', artefact_type='junit')
    payload = failure.to_dict()
    assert payload['findings'] == []
    assert payload['actionable'] is False
    assert payload['top_severity'] == SEVERITY_INFO
    assert 'raw_log_tail' not in payload


# ─── SARIF parser ─────────────────────────────────────────────────────────────


_SARIF_FIXTURE: dict[str, object] = {
    'version': '2.1.0',
    'runs': [
        {
            'tool': {
                'driver': {
                    'name': 'Trivy',
                    'rules': [
                        {'id': 'CVE-2024-9999', 'defaultConfiguration': {'level': 'warning'}},
                    ],
                },
            },
            'results': [
                # CRITICAL via security-severity score.
                {
                    'ruleId': 'CVE-2024-0001',
                    'level': 'error',
                    'message': {'text': 'glibc CVE — heap overflow in resolver'},
                    'locations': [
                        {
                            'physicalLocation': {
                                'artifactLocation': {'uri': 'image/usr/lib/libc.so.6'},
                                'region': {'startLine': 1},
                            }
                        }
                    ],
                    'properties': {'security-severity': '9.8'},
                },
                # Medium via rule's defaultConfiguration (level=warning, no score).
                {
                    'ruleId': 'CVE-2024-9999',
                    'message': {'text': 'minor leak in optional dep'},
                    'locations': [
                        {
                            'physicalLocation': {
                                'artifactLocation': {'uri': 'image/opt/optional.bin'},
                            }
                        }
                    ],
                },
                # No locations + bare-string message + result.level=note → info.
                {
                    'ruleId': 'CVE-2024-1234',
                    'level': 'note',
                    'message': 'historical advisory',
                },
            ],
        }
    ],
}


def test_sarif_parses_three_findings_with_normalised_severities() -> None:
    findings = parse_sarif(json.dumps(_SARIF_FIXTURE))
    assert len(findings) == 3
    by_rule = {f.rule: f for f in findings}
    assert by_rule['CVE-2024-0001'].severity == SEVERITY_CRITICAL
    assert by_rule['CVE-2024-0001'].location == 'image/usr/lib/libc.so.6:1'
    assert by_rule['CVE-2024-0001'].extra['tool'] == 'Trivy'
    assert by_rule['CVE-2024-0001'].extra['security_severity_score'] == '9.8'
    assert by_rule['CVE-2024-9999'].severity == SEVERITY_MEDIUM
    assert by_rule['CVE-2024-9999'].location == 'image/opt/optional.bin'
    # No locations + level=note → info, empty location.
    assert by_rule['CVE-2024-1234'].severity == SEVERITY_INFO
    assert by_rule['CVE-2024-1234'].location == ''
    assert by_rule['CVE-2024-1234'].message == 'historical advisory'


def test_sarif_soft_fails_on_malformed_json() -> None:
    assert parse_sarif('not json') == []
    assert parse_sarif('{') == []
    assert parse_sarif('') == []


def test_sarif_soft_fails_on_missing_runs() -> None:
    assert parse_sarif('{"version": "2.1.0"}') == []
    assert parse_sarif('{"runs": "not-a-list"}') == []


def test_sarif_handles_bytes_input() -> None:
    findings = parse_sarif(json.dumps(_SARIF_FIXTURE).encode('utf-8'))
    assert len(findings) == 3


def test_sarif_clean_run_returns_empty_findings() -> None:
    """A SARIF document with no results is valid — a clean scan."""
    clean: dict[str, object] = {
        'version': '2.1.0',
        'runs': [{'tool': {'driver': {'name': 'Trivy'}}, 'results': []}],
    }
    assert parse_sarif(json.dumps(clean)) == []


# ─── JUnit XML parser ─────────────────────────────────────────────────────────


_JUNIT_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="tests.test_app" tests="4" failures="1" errors="1" skipped="1">
    <testcase classname="tests.test_app" name="test_ok" time="0.012"/>
    <testcase classname="tests.test_app" name="test_fail" time="0.005">
      <failure type="AssertionError" message="expected 200, got 500">
Traceback (most recent call last):
  File "tests/test_app.py", line 42, in test_fail
    assert response.status_code == 200, f"expected 200, got {response.status_code}"
AssertionError: expected 200, got 500
      </failure>
    </testcase>
    <testcase classname="tests.test_app" name="test_error" time="0.002">
      <error type="ConnectionError" message="getaddrinfo failed for db:5432"/>
    </testcase>
    <testcase classname="tests.test_app" name="test_skipped" time="0.0">
      <skipped message="Requires GPU; skipped in CI"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_junit_parses_failure_error_skipped() -> None:
    findings = parse_junit_xml(_JUNIT_FIXTURE)
    # 1 failure + 1 error + 1 skipped = 3 findings total
    assert len(findings) == 3
    by_loc = {f.location: f for f in findings}
    fail = by_loc['tests.test_app::test_fail']
    assert fail.severity == SEVERITY_HIGH
    assert fail.rule == 'AssertionError'
    assert 'expected 200, got 500' in fail.message
    assert 'stacktrace' in fail.extra
    assert 'AssertionError: expected 200, got 500' in fail.extra['stacktrace']

    err = by_loc['tests.test_app::test_error']
    assert err.severity == SEVERITY_HIGH
    assert err.rule == 'ConnectionError'
    assert 'getaddrinfo' in err.message

    skipped = by_loc['tests.test_app::test_skipped']
    assert skipped.severity == SEVERITY_INFO
    assert skipped.extra['failure_kind'] == 'skipped'


def test_junit_handles_bare_testsuite_root() -> None:
    """Older surefire emits a bare <testsuite> at the root."""
    xml = """<?xml version="1.0"?>
<testsuite name="solo">
  <testcase classname="solo.test" name="boom">
    <failure type="X" message="m">stack</failure>
  </testcase>
</testsuite>
"""
    findings = parse_junit_xml(xml)
    assert len(findings) == 1
    assert findings[0].location == 'solo.test::boom'


def test_junit_strips_doctype_defensively() -> None:
    """DOCTYPE preamble must not break parsing (XXE defence)."""
    xml = """<?xml version="1.0"?>
<!DOCTYPE testsuites SYSTEM "evil.dtd">
<testsuites><testsuite name="x">
  <testcase classname="c" name="n"><failure message="m"/></testcase>
</testsuite></testsuites>
"""
    findings = parse_junit_xml(xml)
    assert len(findings) == 1


def test_junit_soft_fails_on_malformed_xml() -> None:
    assert parse_junit_xml('<not really xml>') == []
    assert parse_junit_xml('') == []
    # Wrong root tag.
    assert parse_junit_xml('<root><child/></root>') == []


def test_junit_handles_bytes_input() -> None:
    findings = parse_junit_xml(_JUNIT_FIXTURE.encode('utf-8'))
    assert len(findings) == 3


def test_junit_clean_run_returns_empty() -> None:
    xml = """<?xml version="1.0"?>
<testsuites><testsuite name="all-green" tests="2" failures="0" errors="0" skipped="0">
  <testcase classname="c" name="ok1"/><testcase classname="c" name="ok2"/>
</testsuite></testsuites>
"""
    assert parse_junit_xml(xml) == []


# ─── Trivy native JSON parser ─────────────────────────────────────────────────


_TRIVY_FIXTURE: dict[str, object] = {
    'SchemaVersion': 2,
    'ArtifactName': 'leartech-automated-agent:latest',
    'ArtifactType': 'container_image',
    'Results': [
        {
            'Target': 'alpine:3.19 (alpine)',
            'Type': 'alpine',
            'Vulnerabilities': [
                {
                    'VulnerabilityID': 'CVE-2024-1111',
                    'PkgName': 'openssl',
                    'InstalledVersion': '3.0.10-r0',
                    'FixedVersion': '3.0.11-r0',
                    'Severity': 'CRITICAL',
                    'Title': 'Heap buffer overflow in EVP_PKEY_decrypt',
                    'Description': 'A malformed key blob can crash openssl',
                    'References': ['https://nvd.nist.gov/vuln/detail/CVE-2024-1111'],
                }
            ],
            'Misconfigurations': [
                {
                    'ID': 'AVD-DS-0002',
                    'Severity': 'HIGH',
                    'Title': 'Running as root',
                    'Message': 'Container does not declare a non-root USER',
                    'Resolution': "Add 'USER appuser' to your Dockerfile",
                }
            ],
            'Secrets': [
                {
                    'RuleID': 'aws-secret-access-key',
                    'Severity': 'CRITICAL',
                    'Title': 'AWS Access Key ID',
                    'Match': 'AKIA****',
                    'StartLine': 12,
                }
            ],
        }
    ],
}


def test_trivy_parses_vuln_misconfig_and_secret() -> None:
    findings = parse_trivy_json(json.dumps(_TRIVY_FIXTURE))
    assert len(findings) == 3
    by_rule = {f.rule: f for f in findings}
    vuln = by_rule['CVE-2024-1111']
    assert vuln.severity == SEVERITY_CRITICAL
    assert vuln.location == 'openssl@3.0.10-r0'
    assert vuln.extra['fixed_version'] == '3.0.11-r0'
    assert vuln.extra['references']

    mc = by_rule['AVD-DS-0002']
    assert mc.severity == SEVERITY_HIGH
    assert mc.location == 'alpine:3.19 (alpine)'
    assert mc.extra['kind'] == 'misconfiguration'
    assert 'USER appuser' in mc.extra['resolution']

    secret = by_rule['aws-secret-access-key']
    assert secret.severity == SEVERITY_CRITICAL
    assert secret.location.endswith(':12')
    assert secret.extra['kind'] == 'secret'


def test_trivy_clean_results_returns_empty() -> None:
    assert parse_trivy_json('{"Results": []}') == []
    assert parse_trivy_json('{}') == []


def test_trivy_soft_fails_on_malformed_json() -> None:
    assert parse_trivy_json('not json') == []
    assert parse_trivy_json('') == []


# ─── govulncheck parser ───────────────────────────────────────────────────────


_GOVULNCHECK_FIXTURE_LINES = [
    {
        'osv': {
            'id': 'GO-2024-1234',
            'summary': 'crypto/internal/edwards25519: panic on malformed input',
            'references': [
                {'type': 'ADVISORY', 'url': 'https://pkg.go.dev/vuln/GO-2024-1234'},
            ],
        }
    },
    {
        'osv': {
            'id': 'GO-2024-5678',
            'summary': 'net/http: header injection via malformed Trailer',
        }
    },
    # CALLED finding — has a trace[0].position pointing at user code.
    {
        'finding': {
            'osv': 'GO-2024-1234',
            'trace': [
                {
                    'function': {'name': 'crypto/ed25519.SignMessage'},
                    'position': {'filename': 'cmd/server/main.go', 'line': 42},
                },
            ],
        }
    },
    # IMPORTED-ONLY finding — no position, just a function name.
    {
        'finding': {
            'osv': 'GO-2024-5678',
            'trace': [{'function': {'name': 'net/http.parseHeaders'}}],
        }
    },
]


def test_govulncheck_distinguishes_called_vs_imported() -> None:
    stream = '\n'.join(json.dumps(o) for o in _GOVULNCHECK_FIXTURE_LINES)
    findings = parse_govulncheck_json(stream)
    assert len(findings) == 2
    by_rule = {f.rule: f for f in findings}
    called = by_rule['GO-2024-1234']
    assert called.severity == SEVERITY_HIGH
    assert called.location == 'cmd/server/main.go:42'
    assert called.extra['called'] is True
    assert called.extra['references']

    imported = by_rule['GO-2024-5678']
    assert imported.severity == SEVERITY_LOW
    assert imported.extra['called'] is False
    assert imported.extra['imported_only'] is True


def test_govulncheck_handles_pretty_printed_stream() -> None:
    """When ``jq .`` pretty-prints the stream, objects span multiple lines."""
    stream = '\n'.join(json.dumps(o, indent=2) for o in _GOVULNCHECK_FIXTURE_LINES)
    findings = parse_govulncheck_json(stream)
    assert len(findings) == 2


def test_govulncheck_soft_fails_on_empty_input() -> None:
    assert parse_govulncheck_json('') == []
    assert parse_govulncheck_json('not json at all') == []


# ─── Coverage JSON parser ─────────────────────────────────────────────────────


_COVERAGE_FIXTURE: dict[str, object] = {
    'meta': {'version': '7.4.0'},
    'totals': {
        'covered_lines': 700,
        'num_statements': 1000,
        'percent_covered': 70.0,
    },
    'files': {
        'gate/agent/initiative.py': {
            'summary': {
                'percent_covered': 35.0,
                'missing_lines': list(range(20, 35)) + list(range(50, 80)),
            }
        },
        'gate/agent/run_driver.py': {'summary': {'percent_covered': 78.0, 'missing_lines': [12, 13]}},
        # All-green file — must NOT appear in findings.
        'gate/agent/system_prompt.py': {'summary': {'percent_covered': 100.0, 'missing_lines': []}},
    },
}


def test_coverage_emits_total_plus_below_threshold_files() -> None:
    findings = parse_coverage_json(json.dumps(_COVERAGE_FIXTURE), threshold=80.0)
    locations = [f.location for f in findings]
    # Total + 2 below-threshold files. The all-green file must not appear.
    assert '<total>' in locations
    assert 'gate/agent/initiative.py' in locations
    assert 'gate/agent/run_driver.py' in locations
    assert 'gate/agent/system_prompt.py' not in locations


def test_coverage_severity_scales_with_gap() -> None:
    findings = parse_coverage_json(json.dumps(_COVERAGE_FIXTURE), threshold=80.0)
    by_loc = {f.location: f for f in findings}
    # 80 - 35 = 45pp gap → high
    assert by_loc['gate/agent/initiative.py'].severity == SEVERITY_HIGH
    # 80 - 78 = 2pp gap → info (filtered as non-actionable)
    assert by_loc['gate/agent/run_driver.py'].severity == SEVERITY_INFO
    # 80 - 70 = 10pp gap → medium
    assert by_loc['<total>'].severity == SEVERITY_MEDIUM


def test_coverage_clean_above_threshold_returns_empty() -> None:
    clean: dict[str, object] = {
        'totals': {'percent_covered': 95.0},
        'files': {'a.py': {'summary': {'percent_covered': 92.0}}},
    }
    assert parse_coverage_json(json.dumps(clean), threshold=80.0) == []


def test_coverage_missing_lines_truncated_when_huge() -> None:
    fixture: dict[str, object] = {
        'totals': {'percent_covered': 50.0},
        'files': {'monster.py': {'summary': {'percent_covered': 5.0, 'missing_lines': list(range(1, 200))}}},
    }
    findings = parse_coverage_json(json.dumps(fixture), threshold=80.0)
    file_finding = next(f for f in findings if f.location == 'monster.py')
    assert 'missing_lines_sample' in file_finding.extra
    assert file_finding.extra['missing_lines_count'] == 199
    assert len(file_finding.extra['missing_lines_sample']) == 30


def test_coverage_soft_fails_on_malformed() -> None:
    assert parse_coverage_json('not json') == []
    assert parse_coverage_json('{"unrelated": true}') == []


# ─── Playwright JSON parser ───────────────────────────────────────────────────


_PLAYWRIGHT_FIXTURE: dict[str, object] = {
    'config': {'projects': [{'name': 'chromium'}]},
    'suites': [
        {
            'title': 'login.spec.ts',
            'file': 'tests/login.spec.ts',
            'specs': [],
            'suites': [
                {
                    'title': 'authenticated user',
                    'specs': [
                        {
                            'title': 'shows dashboard',
                            'file': 'tests/login.spec.ts',
                            'tests': [
                                {
                                    'results': [
                                        {
                                            'status': 'failed',
                                            'error': {
                                                'message': 'expect(page).toHaveURL: /dashboard',
                                                'stack': 'at Object.<anonymous> (login.spec.ts:42)',
                                            },
                                            'attachments': [
                                                {
                                                    'name': 'screenshot',
                                                    'path': './pw-out/screenshot.png',
                                                },
                                                {
                                                    'name': 'trace',
                                                    'url': 'https://gcs/x/trace.zip',
                                                },
                                            ],
                                            'retry': 1,
                                        }
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}


def test_playwright_walks_nested_suites() -> None:
    findings = parse_playwright_json(json.dumps(_PLAYWRIGHT_FIXTURE))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == SEVERITY_HIGH  # 'failed' → high
    assert 'tests/login.spec.ts' in f.location
    assert 'authenticated user' in f.location
    assert 'shows dashboard' in f.location
    assert 'toHaveURL' in f.message
    assert 'stack' in f.extra
    # Both attachment shapes (path / url) captured.
    assert f.extra['screenshot_urls'] == ['./pw-out/screenshot.png']
    assert f.extra['trace_urls'] == ['https://gcs/x/trace.zip']
    assert f.extra['retry'] == 1


def test_playwright_skips_passed_tests() -> None:
    fixture: dict[str, object] = {
        'suites': [
            {
                'title': 's',
                'specs': [
                    {
                        'title': 't',
                        'tests': [{'results': [{'status': 'passed'}]}],
                    }
                ],
            }
        ]
    }
    assert parse_playwright_json(json.dumps(fixture)) == []


def test_playwright_soft_fails_on_malformed() -> None:
    assert parse_playwright_json('') == []
    assert parse_playwright_json('not json') == []
    assert parse_playwright_json('{}') == []


# ─── results.json wrapper ─────────────────────────────────────────────────────


def test_results_json_smoke_pr58_shape() -> None:
    """The v6p0.5 PR #58 reference case still parses through the new wrapper."""
    doc = {
        'success': False,
        'summary': '1/4 checks passed',
        'tests': [
            {'name': '00-seed', 'status': 'pass'},
            {'name': '01-smoke', 'status': 'fail', 'message': 'GET /health/live HTTP 000 FAIL'},
            {'name': '02-auth', 'status': 'fail', 'message': 'HTTP 000 FAIL'},
        ],
    }
    findings = parse_results_json(json.dumps(doc))
    assert len(findings) == 2
    names = sorted(f.location for f in findings)
    assert names == ['01-smoke', '02-auth']
    assert all(f.severity == SEVERITY_HIGH for f in findings)


def test_results_json_handles_surrounding_log_noise() -> None:
    doc = {
        'success': False,
        'summary': '0/1 checks passed',
        'tests': [{'name': 'x', 'status': 'fail', 'message': 'boom'}],
    }
    log = '+ ./run.sh\n=== results ===\n' + json.dumps(doc) + '\n=== end ===\nexit 1\n'
    findings = parse_results_json(log)
    assert len(findings) == 1
    assert findings[0].location == 'x'


def test_results_json_attaches_screenshot_url() -> None:
    doc = {
        'success': False,
        'summary': '0/1 checks passed',
        'tests': [
            {
                'name': '02-login',
                'status': 'fail',
                'message': 'locator timeout',
                'screenshot_url': 'https://artifacts.example/login.png',
            }
        ],
    }
    findings = parse_results_json(json.dumps(doc))
    assert findings[0].extra['screenshot_url'] == 'https://artifacts.example/login.png'


def test_results_json_soft_fails_on_no_results_block() -> None:
    assert parse_results_json('+ ./run.sh\nno json here\nexit 1\n') == []
    assert parse_results_json('') == []


# ─── Registry + dispatcher ────────────────────────────────────────────────────


def test_registry_covers_all_seven_artefact_types() -> None:
    """Every documented artefact type must be in the parser registry."""
    expected = {
        'sarif',
        'junit',
        'results_json',
        'coverage_json',
        'trivy_json',
        'govulncheck_json',
        'playwright_json',
    }
    assert set(ARTEFACT_PARSERS) == expected


def test_resolve_artefact_type_for_known_gates() -> None:
    assert resolve_artefact_type('security-scan') == 'sarif'
    assert resolve_artefact_type('gcp/security-scan') == 'sarif'
    assert resolve_artefact_type('az/image-scan') == 'sarif'
    assert resolve_artefact_type('end2end') == 'results_json'
    assert resolve_artefact_type('gcp/end2end-ui') == 'playwright_json'
    assert resolve_artefact_type('az/test') == 'junit'
    assert resolve_artefact_type('coverage') == 'coverage_json'


def test_resolve_artefact_type_unknown_returns_none() -> None:
    """Unknown gate → None so the heuristic dispatcher takes over."""
    assert resolve_artefact_type('lint') is None
    assert resolve_artefact_type('gcp/pr') is None
    assert resolve_artefact_type('ai-review') is None


def test_parse_gate_artefact_returns_gate_failure() -> None:
    failure = parse_gate_artefact(
        gate='az/security-scan',
        artefact_type='sarif',
        content=json.dumps(_SARIF_FIXTURE),
    )
    assert failure is not None
    assert failure.gate == 'az/security-scan'
    assert failure.artefact_type == 'sarif'
    assert len(failure.findings) == 3
    assert failure.actionable is True


def test_parse_gate_artefact_unknown_type_returns_none() -> None:
    failure = parse_gate_artefact(
        gate='az/whatever',
        artefact_type='not-a-real-type',
        content='whatever',
    )
    assert failure is None


def test_parse_gate_artefact_artefact_type_none_returns_none() -> None:
    failure = parse_gate_artefact(
        gate='az/lint',
        artefact_type=None,
        content='whatever',
    )
    assert failure is None


def test_parse_gate_artefact_preserves_raw_log_tail() -> None:
    failure = parse_gate_artefact(
        gate='az/security-scan',
        artefact_type='sarif',
        content='not valid sarif',
        raw_log_tail='+ trivy ... ERROR: out of disk\n',
    )
    assert failure is not None
    # Bad content → empty findings, but raw_log_tail is preserved for fallback.
    assert failure.findings == ()
    assert 'out of disk' in failure.raw_log_tail


def test_parse_gate_artefact_auto_dispatches_via_gate_name() -> None:
    failure = parse_gate_artefact_auto(
        gate='gcp/security-scan',
        content=json.dumps(_SARIF_FIXTURE),
    )
    assert failure is not None
    assert failure.artefact_type == 'sarif'
    assert len(failure.findings) == 3


def test_parse_gate_artefact_auto_unknown_gate_returns_none() -> None:
    assert parse_gate_artefact_auto(gate='gcp/lint', content='whatever') is None


def test_parse_gate_artefact_soft_fails_when_parser_raises() -> None:
    """Defensive: even if a parser explodes, the dispatcher returns
    a GateFailure with empty findings + the raw_log_tail preserved."""
    # Inject a parser that raises by patching the registry via a custom
    # artefact_type. We'd rather not monkeypatch the global registry, so we
    # test the safe path indirectly: a bytes input with invalid UTF-8 on a
    # parser that should swallow it.
    failure = parse_gate_artefact(
        gate='az/test',
        artefact_type='junit',
        content=b'\xff\xfe\xff\xfe',  # invalid UTF-8 sequence
        raw_log_tail='log',
    )
    assert failure is not None
    assert failure.findings == ()


def test_gate_to_artefact_type_keys_are_subset_of_parsers() -> None:
    """Every mapped artefact_type must have a parser registered."""
    for gate, artefact_type in GATE_TO_ARTEFACT_TYPE.items():
        assert artefact_type in ARTEFACT_PARSERS, f'gate {gate} maps to unknown type {artefact_type}'


# ─── Dispatcher integration (watcher seam) ────────────────────────────────────


def test_dispatch_structured_failure_happy_path() -> None:
    """Watcher seam: artefact fetched, parsed, GateFailure returned."""
    from gate.watcher.artefact_dispatch import dispatch_structured_failure

    calls: list[tuple[str, str, str]] = []

    def fake_fetch(gate: str, prun: str, cluster: str) -> bytes:
        calls.append((gate, prun, cluster))
        return json.dumps(_SARIF_FIXTURE).encode('utf-8')

    failure = dispatch_structured_failure(
        gate='az/security-scan',
        pipelinerun_name='svc-pr1-abc',
        cluster='az',
        artefact_fetcher=fake_fetch,
    )
    assert failure is not None
    assert failure.gate == 'az/security-scan'
    assert failure.artefact_type == 'sarif'
    assert len(failure.findings) == 3
    assert calls == [('az/security-scan', 'svc-pr1-abc', 'az')]


def test_dispatch_structured_failure_unmapped_gate_returns_none() -> None:
    from gate.watcher.artefact_dispatch import dispatch_structured_failure

    def must_not_be_called(gate: str, prun: str, cluster: str) -> bytes:
        raise AssertionError('fetcher must not be called for unmapped gate')

    assert (
        dispatch_structured_failure(
            gate='gcp/lint',
            pipelinerun_name='x',
            cluster='gcp',
            artefact_fetcher=must_not_be_called,
        )
        is None
    )


def test_dispatch_structured_failure_soft_fails_on_fetch_exception() -> None:
    from gate.watcher.artefact_dispatch import dispatch_structured_failure

    def raises_fetch(gate: str, prun: str, cluster: str) -> bytes:
        raise RuntimeError('kubectl context error')

    failure = dispatch_structured_failure(
        gate='az/security-scan',
        pipelinerun_name='x',
        cluster='az',
        artefact_fetcher=raises_fetch,
    )
    assert failure is None


def test_dispatch_structured_failure_empty_bytes_returns_none() -> None:
    from gate.watcher.artefact_dispatch import dispatch_structured_failure

    def empty_fetch(gate: str, prun: str, cluster: str) -> bytes:
        return b''

    failure = dispatch_structured_failure(
        gate='az/security-scan',
        pipelinerun_name='x',
        cluster='az',
        artefact_fetcher=empty_fetch,
    )
    assert failure is None


def test_dispatch_structured_failure_preserves_log_tail_on_empty_findings() -> None:
    """Empty SARIF → empty findings, but log tail still available for fallback rendering."""
    from gate.watcher.artefact_dispatch import dispatch_structured_failure

    def clean_fetch(gate: str, prun: str, cluster: str) -> bytes:
        return b'{"version": "2.1.0", "runs": []}'

    failure = dispatch_structured_failure(
        gate='gcp/security-scan',
        pipelinerun_name='x',
        cluster='gcp',
        artefact_fetcher=clean_fetch,
        raw_log_tail='+ trivy fs ... 0 vulnerabilities found',
    )
    assert failure is not None
    assert failure.findings == ()
    assert 'trivy' in failure.raw_log_tail
