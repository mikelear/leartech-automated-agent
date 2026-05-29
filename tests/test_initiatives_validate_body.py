"""Tests for Phase D.6.1 — POST /initiatives/_validate (YAML body).

Sibling of ``test_app_initiatives.test_validate_endpoint_*`` which covers
the name-based ``GET /_validate/{name}`` endpoint. This module exercises
the body-based companion that lets operators pre-flight a draft YAML
without registering it in the catalog.

Also covers the ``gate.initiatives.validate_cli`` local CLI module which
the ``scripts/validate_initiative.sh`` wrapper invokes — the operator's
no-Python-imports path to the same validation.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# Minimal valid initiative YAML, legacy single-repo shape. Mirrors the
# shape under initiatives/_templates/ so the test is anchored to a real,
# accepted YAML rather than a fabricated one.
_VALID_BODY = textwrap.dedent("""\
    name: probe-initiative-xyz
    repo: leartech-test
    branch: agent/probe
    base: main
    goal: Validate-body endpoint probe — does not register anywhere.
""")


# ─── Happy path ──────────────────────────────────────────────────────────────


def test_post_validate_valid_body_returns_200_with_summary() -> None:
    """Valid YAML body returns 200 + summary dict (same shape as GET /_validate/{name})."""
    resp = client.post('/initiatives/_validate', content=_VALID_BODY, headers={'Content-Type': 'text/plain'})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['name'] == 'probe-initiative-xyz'
    assert body['repos'] == [{'repo': 'leartech-test', 'branch': 'agent/probe', 'base': 'main'}]
    assert body['primary'] == {'repo': 'leartech-test', 'branch': 'agent/probe', 'base': 'main'}
    assert body['gate_marks'] == []
    assert body['max_iterations'] == 5


def test_post_validate_summary_shape_matches_get_validate() -> None:
    """POST /_validate and GET /_validate/{name} must return identical summary shapes.

    Both endpoints share the ``_summary_of`` helper — this test locks the
    invariant in so a future refactor that diverges them fails loudly.
    """
    # Pick any filesystem name that's known to exist; the 404-with-available
    # path is the documented discovery mechanism (see test_app_initiatives).
    discover = client.post('/initiatives', json={'initiative': 'does-not-exist-xyz'})
    target = discover.json()['detail']['available'][0]
    get_resp = client.get(f'/initiatives/_validate/{target}')
    assert get_resp.status_code == 200

    # Now load the same YAML body and POST it — keys should match exactly.
    yaml_body = Path('initiatives') / f'{target}.yaml'
    post_resp = client.post(
        '/initiatives/_validate',
        content=yaml_body.read_text(),
        headers={'Content-Type': 'text/plain'},
    )
    assert post_resp.status_code == 200
    assert set(get_resp.json().keys()) == set(post_resp.json().keys()), (
        'POST /_validate and GET /_validate/{name} must return identical key shapes'
    )


# ─── Error surfaces ──────────────────────────────────────────────────────────


def test_post_validate_invalid_yaml_returns_422() -> None:
    """Malformed YAML returns 422 with ``Invalid initiative YAML`` in detail."""
    bad = 'name: [unclosed bracket'
    resp = client.post('/initiatives/_validate', content=bad, headers={'Content-Type': 'text/plain'})
    assert resp.status_code == 422
    assert 'Invalid initiative YAML' in resp.json()['detail']


def test_post_validate_empty_body_returns_422() -> None:
    """Empty body returns 422.

    Truly-empty requests trip FastAPI's own ``Body(...)`` required-field
    check (structured-error 422); whitespace-only bodies trip our own
    handler guard with ``Invalid initiative YAML: empty body``. Both are
    422 — that's the only contract the test pins.
    """
    resp = client.post('/initiatives/_validate', content='', headers={'Content-Type': 'text/plain'})
    assert resp.status_code == 422

    # Whitespace-only body: our handler's guard fires.
    resp2 = client.post('/initiatives/_validate', content='   \n  ', headers={'Content-Type': 'text/plain'})
    assert resp2.status_code == 422
    assert 'Invalid initiative YAML' in resp2.json()['detail']


def test_post_validate_schema_violation_returns_422() -> None:
    """A YAML mapping missing required fields (``goal``) fails Pydantic validation
    and surfaces as 422 with the violation in the detail string."""
    bad = textwrap.dedent("""\
        name: missing-goal
        repo: leartech-test
        branch: agent/missing-goal
    """)
    resp = client.post('/initiatives/_validate', content=bad, headers={'Content-Type': 'text/plain'})
    assert resp.status_code == 422
    detail = resp.json()['detail']
    assert 'Invalid initiative YAML' in detail
    # Pydantic's ValidationError stringifies with the field name in the message.
    assert 'goal' in detail.lower()


# ─── No side effects ─────────────────────────────────────────────────────────


def test_post_validate_does_not_add_to_catalog_or_state() -> None:
    """Calling POST /_validate twice must not enqueue or register anything.

    Pre-flight validation is read-only. Snapshot the initiatives list before
    and after — count must be identical.
    """
    before = client.get('/initiatives').json()
    for _ in range(2):
        resp = client.post(
            '/initiatives/_validate',
            content=_VALID_BODY,
            headers={'Content-Type': 'text/plain'},
        )
        assert resp.status_code == 200
    after = client.get('/initiatives').json()
    assert len(before) == len(after), 'POST /_validate must not register anything in app.state'


# ─── gate.initiatives.validate_cli ───────────────────────────────────────────


def test_validate_cli_ok_on_valid_yaml(tmp_path: Path) -> None:
    """The CLI module prints ``OK: <name>`` and exits 0 on a valid YAML file."""
    yaml_file = tmp_path / 'probe.yaml'
    yaml_file.write_text(_VALID_BODY)
    result = subprocess.run(
        [sys.executable, '-m', 'gate.initiatives.validate_cli', str(yaml_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert 'OK: probe-initiative-xyz' in result.stdout
    assert 'repo=mikelear/leartech-test branch=agent/probe' in result.stdout


def test_validate_cli_fails_on_invalid_yaml(tmp_path: Path) -> None:
    """The CLI module exits 1 with ``FAIL: ...`` on stderr for invalid YAML."""
    yaml_file = tmp_path / 'broken.yaml'
    yaml_file.write_text('name: [unclosed')
    result = subprocess.run(
        [sys.executable, '-m', 'gate.initiatives.validate_cli', str(yaml_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert 'FAIL' in result.stderr


def test_validate_cli_usage_exit_code(tmp_path: Path) -> None:
    """Wrong arg count exits 2 with usage on stderr."""
    result = subprocess.run(
        [sys.executable, '-m', 'gate.initiatives.validate_cli'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert 'Usage' in result.stderr
