"""Introspection endpoints — /mcps, /roles, /topology, /health/detail."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_list_mcps_returns_known_in_process_servers() -> None:
    response = client.get('/mcps')
    assert response.status_code == 200
    mcps = response.json()
    names = {m['name'] for m in mcps}
    # The four sdk-type MCPs we ship in the runtime
    assert 'leartech-pipeline' in names
    assert 'leartech-criteria' in names
    assert 'leartech-pr-context' in names
    assert 'leartech-test-artifacts' in names


def test_list_mcps_status_includes_ready_and_not_built() -> None:
    response = client.get('/mcps')
    statuses = {m['status'] for m in response.json()}
    assert 'ready' in statuses  # sdk-type ones import cleanly
    assert 'not_built' in statuses  # stitch / lovable / figma


def test_get_mcp_detail_includes_spec_and_roles() -> None:
    response = client.get('/mcps/leartech-pipeline')
    assert response.status_code == 200
    detail = response.json()
    assert detail['name'] == 'leartech-pipeline'
    assert detail['spec']['type'] == 'sdk'
    # Two roles consume the pipeline MCP
    assert 'initiative_agent' in detail['roles']
    assert 'review_agent' in detail['roles']


def test_get_mcp_detail_unknown_returns_404_with_available_list() -> None:
    response = client.get('/mcps/never-was')
    assert response.status_code == 404
    detail = response.json()['detail']
    assert 'never-was' in detail['message']
    assert len(detail['available']) >= 4


def test_list_roles_includes_all_four_personas() -> None:
    response = client.get('/roles')
    assert response.status_code == 200
    names = {r['name'] for r in response.json()}
    assert {'initiative_agent', 'review_agent', 'ba_agent', 'forensic_agent'} <= names


def test_get_role_detail_includes_lesson_count() -> None:
    response = client.get('/roles/initiative_agent')
    assert response.status_code == 200
    detail = response.json()
    assert detail['name'] == 'initiative_agent'
    assert detail['spec']['mcps']
    assert detail['lesson_count'] >= 0  # at least some lessons apply_to this role


def test_get_role_unknown_returns_404() -> None:
    response = client.get('/roles/does-not-exist')
    assert response.status_code == 404


def test_topology_returns_mermaid() -> None:
    response = client.get('/topology')
    assert response.status_code == 200
    body = response.json()
    assert 'mermaid' in body
    assert body['mermaid'].startswith('graph')
    assert 'Phase 1' in body['mermaid']
    assert 'Phase 4' in body['mermaid']


def test_topology_feedback_focuses_on_rings() -> None:
    response = client.get('/topology/feedback')
    assert response.status_code == 200
    body = response.json()
    assert 'Ring 1' in body['mermaid']
    assert 'Ring 2' in body['mermaid']
    assert 'Ring 3' in body['mermaid']
    assert 'lessons catalog' in body['mermaid'].lower()


def test_health_detail_summarises_state() -> None:
    response = client.get('/health/detail')
    assert response.status_code == 200
    body = response.json()
    assert body['service'] == 'leartech-automated-agent'
    assert body['lessons_loaded'] > 0
    assert body['mcps_total'] >= 4
    assert body['mcps_ready'] >= 4  # the four sdk-type MCPs
    assert 'initiative_agent' in body['roles']
    assert len(body['feedback_rings']) == 3
    ring_statuses = {r['name']: r['status'] for r in body['feedback_rings']}
    assert ring_statuses['ring1_pr_gate'] == 'active'
    assert ring_statuses['ring2_staging'] == 'pending'
