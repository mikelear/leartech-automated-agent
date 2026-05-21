"""Tests for MCP admin endpoints: POST /mcps, DELETE /mcps/{name}, PUT /mcps/{name}/roles."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from gate.agent.mcp_catalog import Catalog, load_catalog

client = TestClient(app)

_MOCK_PR = {
    'pr_url': 'https://github.com/mikelear/leartech-automated-agent/pull/99',
    'pr_number': 99,
    'branch': 'agent/mcp-add-test-mcp-abc12345',
}


@pytest.fixture(autouse=True)
def _clear_catalog_cache() -> Iterator[None]:
    """Clear the catalog lru_cache before and after each test for isolation."""
    load_catalog.cache_clear()
    yield
    load_catalog.cache_clear()


# ─── POST /mcps ───────────────────────────────────────────────────────────────


def test_post_mcps_sdk_requires_builder() -> None:
    resp = client.post(
        '/mcps',
        json={
            'name': 'new-sdk-mcp',
            'type': 'sdk',
            'description': 'Test SDK MCP without builder',
        },
    )
    assert resp.status_code == 422


def test_post_mcps_stdio_requires_command() -> None:
    resp = client.post(
        '/mcps',
        json={
            'name': 'new-stdio-mcp',
            'type': 'stdio',
            'description': 'Test stdio MCP without command',
        },
    )
    assert resp.status_code == 422


def test_post_mcps_http_sse_requires_url() -> None:
    resp = client.post(
        '/mcps',
        json={
            'name': 'new-sse-mcp',
            'type': 'http_sse',
            'description': 'Test http_sse MCP without url',
        },
    )
    assert resp.status_code == 422


def test_post_mcps_rejects_duplicate_name() -> None:
    # leartech-pipeline is already in the committed catalog
    resp = client.post(
        '/mcps',
        json={
            'name': 'leartech-pipeline',
            'type': 'sdk',
            'builder': 'some.module:some_fn',
            'description': 'Duplicate attempt',
        },
    )
    assert resp.status_code == 409
    assert 'leartech-pipeline' in resp.json()['detail']


def test_post_mcps_opens_pr_on_valid() -> None:
    with patch(
        'app.routers.mcp_admin.open_yaml_change_pr',
        new_callable=AsyncMock,
        return_value=_MOCK_PR,
    ) as mock_pr:
        resp = client.post(
            '/mcps',
            json={
                'name': 'brand-new-mcp',
                'type': 'sdk',
                'builder': 'some.module:build_fn',
                'description': 'A brand new test MCP',
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body['pr_url'] == _MOCK_PR['pr_url']
    assert body['pr_number'] == _MOCK_PR['pr_number']
    assert 'brand-new-mcp' in body['change_summary']
    mock_pr.assert_awaited_once()
    # Verify the call included the right repo + file
    call_kwargs = mock_pr.call_args.kwargs
    assert call_kwargs['repo'] == 'leartech-automated-agent'
    assert call_kwargs['file_path'] == 'gate/agent/mcp_catalog.yaml'


# ─── DELETE /mcps/{name} ─────────────────────────────────────────────────────


def test_delete_mcps_404_if_not_found() -> None:
    resp = client.delete('/mcps/nonexistent-mcp-xyz')
    assert resp.status_code == 404
    assert 'nonexistent-mcp-xyz' in resp.json()['detail']


def test_delete_mcps_409_if_role_uses_it() -> None:
    # leartech-pipeline is referenced by initiative_agent, review_agent, forensic_agent
    resp = client.delete('/mcps/leartech-pipeline')
    assert resp.status_code == 409
    assert 'initiative_agent' in resp.json()['detail']


def test_delete_mcps_opens_pr_on_valid() -> None:
    # Use a synthetic catalog where 'orphan-mcp' exists but no role references it
    synthetic_catalog = Catalog.model_validate(
        {
            'mcp_servers': {
                'orphan-mcp': {'type': 'stdio', 'command': 'foo', 'description': 'unreferenced'},
            },
            'roles': {
                'review_agent': {'description': 'test role', 'mcps': [], 'tools': []},
            },
        }
    )
    synthetic_raw: dict[str, object] = {
        'mcp_servers': {
            'orphan-mcp': {'type': 'stdio', 'command': 'foo', 'description': 'unreferenced'},
        },
        'roles': {
            'review_agent': {'description': 'test role', 'mcps': [], 'tools': []},
        },
    }

    with (
        patch('app.routers.mcp_admin.load_catalog', return_value=synthetic_catalog),
        patch('app.routers.mcp_admin._read_raw_catalog', return_value=synthetic_raw),
        patch(
            'app.routers.mcp_admin.open_yaml_change_pr',
            new_callable=AsyncMock,
            return_value=_MOCK_PR,
        ) as mock_pr,
    ):
        resp = client.delete('/mcps/orphan-mcp')

    assert resp.status_code == 200
    body = resp.json()
    assert body['pr_url'] == _MOCK_PR['pr_url']
    assert body['pr_number'] == _MOCK_PR['pr_number']
    assert 'orphan-mcp' in body['change_summary']
    mock_pr.assert_awaited_once()


# ─── PUT /mcps/{name}/roles ───────────────────────────────────────────────────


def test_put_roles_404_if_mcp_not_found() -> None:
    resp = client.put('/mcps/nonexistent-mcp-xyz/roles', json={'grant': ['initiative_agent']})
    assert resp.status_code == 404
    assert 'nonexistent-mcp-xyz' in resp.json()['detail']


def test_put_roles_400_if_role_does_not_exist() -> None:
    # leartech-criteria exists in catalog; fake_role does not
    resp = client.put('/mcps/leartech-criteria/roles', json={'grant': ['fake_role']})
    assert resp.status_code == 400
    assert 'fake_role' in resp.json()['detail']


def test_put_roles_400_if_already_granted() -> None:
    # leartech-pipeline is already in initiative_agent.mcps
    resp = client.put('/mcps/leartech-pipeline/roles', json={'grant': ['initiative_agent']})
    assert resp.status_code == 400
    assert 'already' in resp.json()['detail']


def test_put_roles_400_if_revoking_ungranted() -> None:
    # leartech-pipeline is NOT in ba_agent.mcps
    resp = client.put('/mcps/leartech-pipeline/roles', json={'revoke': ['ba_agent']})
    assert resp.status_code == 400
    assert 'not in role' in resp.json()['detail']


def test_put_roles_opens_pr_on_valid_grant() -> None:
    # leartech-criteria is NOT in review_agent.mcps, so granting is valid
    with patch(
        'app.routers.mcp_admin.open_yaml_change_pr',
        new_callable=AsyncMock,
        return_value=_MOCK_PR,
    ) as mock_pr:
        resp = client.put('/mcps/leartech-criteria/roles', json={'grant': ['review_agent']})

    assert resp.status_code == 200
    body = resp.json()
    assert body['pr_url'] == _MOCK_PR['pr_url']
    assert body['pr_number'] == _MOCK_PR['pr_number']
    assert 'leartech-criteria' in body['change_summary']
    assert 'grant' in body['change_summary']
    mock_pr.assert_awaited_once()


def test_put_roles_opens_pr_on_valid_revoke() -> None:
    # leartech-pipeline IS in review_agent.mcps, so revoking is valid
    with patch(
        'app.routers.mcp_admin.open_yaml_change_pr',
        new_callable=AsyncMock,
        return_value=_MOCK_PR,
    ) as mock_pr:
        resp = client.put('/mcps/leartech-pipeline/roles', json={'revoke': ['review_agent']})

    assert resp.status_code == 200
    body = resp.json()
    assert body['pr_url'] == _MOCK_PR['pr_url']
    assert body['pr_number'] == _MOCK_PR['pr_number']
    assert 'leartech-pipeline' in body['change_summary']
    assert 'revoke' in body['change_summary']
    mock_pr.assert_awaited_once()
