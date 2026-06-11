"""Ambiguous ``--cluster`` prefixes raise with a disambiguation hint.

Today there are only two clusters (``gcp-staging`` / ``az-staging``)
and any single-letter prefix resolves uniquely. The defensive part:
once ``gcp-prod`` lands next to ``gcp-staging``, ``--cluster gcp``
becomes ambiguous and must error rather than silently pick one.

We pin the contract with a constructed config (no need to wait for the
real prod cluster to exist).
"""

from __future__ import annotations

import pytest

from app.agent_cli.config import CliConfig, ClusterConfig


def _multi_gcp_config() -> CliConfig:
    """Construct a config with two clusters that share a prefix."""
    clusters = {
        'gcp-staging': ClusterConfig(
            name='gcp-staging',
            orch_url='https://orch-staging.example.com',
            agent_url='https://agent-staging.example.com',
        ),
        'gcp-prod': ClusterConfig(
            name='gcp-prod',
            orch_url='https://orch-prod.example.com',
            agent_url='https://agent-prod.example.com',
        ),
        'az-staging': ClusterConfig(
            name='az-staging',
            orch_url='https://orch-az.example.com',
            agent_url='https://agent-az.example.com',
        ),
    }
    return CliConfig(default_cluster='gcp-staging', clusters=clusters)


def test_ambiguous_prefix_raises_listing_matches() -> None:
    """``gcp`` matches both ``gcp-staging`` and ``gcp-prod`` → error."""
    cfg = _multi_gcp_config()
    with pytest.raises(ValueError) as exc:
        cfg.resolve_cluster('gcp')
    msg = str(exc.value)
    assert 'Ambiguous' in msg
    # Both candidates must appear so the operator can pick the right one.
    assert 'gcp-staging' in msg
    assert 'gcp-prod' in msg


def test_ambiguous_prefix_does_not_silently_pick_one() -> None:
    """Make sure the error path runs — neither URL is returned by accident."""
    cfg = _multi_gcp_config()
    with pytest.raises(ValueError):
        cfg.resolve_cluster('gcp')


def test_exact_match_wins_over_prefix_match() -> None:
    """If a cluster's full name happens to be the prefix of another, exact match wins.

    Defensive: an operator who literally configured a cluster named
    ``gcp`` should still be able to address it as ``gcp`` without
    tripping the ambiguity guard (which only fires when the requested
    name is *not* itself a configured key).
    """
    clusters = {
        'gcp': ClusterConfig(name='gcp', orch_url='https://o.example.com', agent_url='https://a.example.com'),
        'gcp-staging': ClusterConfig(
            name='gcp-staging',
            orch_url='https://o-staging.example.com',
            agent_url='https://a-staging.example.com',
        ),
    }
    cfg = CliConfig(default_cluster='gcp', clusters=clusters)
    resolved = cfg.resolve_cluster('gcp')
    assert resolved.name == 'gcp'
    assert resolved.agent_url == 'https://a.example.com'


def test_unambiguous_prefix_among_three_clusters_resolves() -> None:
    """When only one configured name starts with the prefix, that name wins
    even with multiple clusters present."""
    cfg = _multi_gcp_config()
    resolved = cfg.resolve_cluster('az')
    assert resolved.name == 'az-staging'
