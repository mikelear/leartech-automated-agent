"""Introspection endpoints — /mcps, /roles, /topology, /health/detail."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state import InitiativeRecord, _records, new_id, register

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


def test_mcp_health_probe_for_sdk_returns_ready() -> None:
    response = client.get('/mcps/leartech-pipeline/health')
    assert response.status_code == 200
    body = response.json()
    assert body['name'] == 'leartech-pipeline'
    assert body['status'] == 'ready'
    assert body['probe'] == 'sdk_import'


def test_mcp_health_probe_unknown_returns_404() -> None:
    response = client.get('/mcps/never-was/health')
    assert response.status_code == 404


@pytest.fixture
def _seeded_run() -> str:
    """Register a fake run record so the timeline / why endpoints have something to render.

    Uses the in-memory ``_records`` store (no DB configured in tests),
    which the same ``app.state.get`` reads from.
    """
    run_id = new_id()
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)
    # Use register() to mirror prod path; falls back to _records when DB disabled.
    import asyncio

    asyncio.run(
        register(
            InitiativeRecord(
                id=run_id,
                initiative='test-init',
                status='complete',
                started_at=started,
                started_executing_at=started,
                finished_at=finished,
                pr_number=42,
                pr_repo='mikelear/test-repo',
                turns=3,
                cost_usd=0.0123,
            )
        )
    )
    yield run_id
    _records.pop(run_id, None)


def test_initiative_timeline_includes_lifecycle_events(_seeded_run: str) -> None:
    response = client.get(f'/initiatives/{_seeded_run}/timeline')
    assert response.status_code == 200
    body = response.json()
    assert body['run_id'] == _seeded_run
    assert body['initiative'] == 'test-init'
    kinds = [event['kind'] for event in body['events']]
    assert 'registered' in kinds
    assert 'first_turn' in kinds
    assert 'pr_opened' in kinds
    assert 'finished' in kinds


def test_initiative_timeline_unknown_returns_404() -> None:
    response = client.get('/initiatives/does-not-exist-xyz/timeline')
    assert response.status_code == 404


def test_initiative_why_returns_matched_lesson_ids(_seeded_run: str) -> None:
    response = client.get(f'/initiatives/{_seeded_run}/why')
    assert response.status_code == 200
    body = response.json()
    assert body['run_id'] == _seeded_run
    # At least one calibration lesson applies to initiative_agent in the catalog.
    assert body['matched_count'] > 0
    assert len(body['matched_lessons']) == body['matched_count']


def test_initiative_why_unknown_returns_404() -> None:
    response = client.get('/initiatives/does-not-exist-xyz/why')
    assert response.status_code == 404
