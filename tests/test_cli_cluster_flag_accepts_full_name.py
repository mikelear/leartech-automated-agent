"""``--cluster <full-name>`` resolves to the configured staging URL.

Pins the contract surfaced by ``cli-fix-cluster-flag-name-mismatch``:
the same name ``config show`` reports (``gcp-staging``) must be
accepted by the top-level ``--cluster`` flag.

Before the fix, the Click choice was ``['gcp', 'az']`` which rejected
``gcp-staging`` with ``Invalid value for '--cluster'``. After, any
configured cluster name (or unambiguous prefix) is accepted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_cli.transport import resolve_base_url


def test_cluster_full_name_gcp_staging_resolves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--cluster gcp-staging`` returns the built-in GCP staging URL."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('LEARTECH_AGENT_URL', raising=False)
    resolved = resolve_base_url(url=None, cluster='gcp-staging')
    assert resolved == 'https://leartech-automated-agent-jx-staging.jx.leartech.com'


def test_cluster_full_name_az_staging_resolves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--cluster az-staging`` returns the built-in Azure staging URL."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('LEARTECH_AGENT_URL', raising=False)
    resolved = resolve_base_url(url=None, cluster='az-staging')
    assert resolved == 'https://leartech-automated-agent-jx-staging.az.leartech.com'
