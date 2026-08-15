"""Cluster-aware configuration for the ``leartech-agent`` CLI.

Operators install the CLI once (via ``pipx``) and then point it at any
cluster's agent ingress without editing source. The resolution priority
for ``agent_url`` is:

1. The ``--url`` flag on the invocation (highest).
2. The ``LEARTECH_AGENT_URL`` env var.
3. The cluster picked by ``--cluster`` (or, absent that, the
   ``default_cluster`` in the config file) — looked up in the
   ``clusters:`` map of ``~/.config/leartech-agent/config.yaml``.
4. A built-in default (staging URL pattern), if a known cluster key
   was named.
5. ``http://localhost:8080`` laptop fallback.

The config file uses the shape:

.. code-block:: yaml

    default_cluster: gcp-staging
    clusters:
      gcp-staging:
        agent_url: https://leartech-automated-agent-jx-staging.jx.leartech.com
      az-staging:
        agent_url: https://leartech-automated-agent-jx-staging.az.leartech.com

Legacy note — ``orch_url``: earlier versions of this CLI also carried an
``orch_url`` field pointing at the ``leartech-orchestrator`` service.
That service has been decommissioned. Existing on-disk configs may still
carry the key; :func:`load_config` reads and discards it silently rather
than erroring, so an operator's pipx install keeps working across the
transition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Where the per-user config lives. ``XDG_CONFIG_HOME`` is honoured first
# (Linux convention); falls back to ``~/.config`` which works on macOS
# and Windows WSL alike.
DEFAULT_CONFIG_DIR_ENV = 'XDG_CONFIG_HOME'
APP_NAME = 'leartech-agent'
CONFIG_FILENAME = 'config.yaml'

# Built-in cluster defaults — shipped with the CLI so a fresh install can
# reach staging on first invocation without any config file. The matching
# cluster keys are ``<cloud>-<env>`` to keep the namespace open for
# ``gcp-prod`` / ``az-prod`` once those land. Operators override by
# writing the same key into ``~/.config/leartech-agent/config.yaml``.
_BUILTIN_CLUSTERS: dict[str, dict[str, str]] = {
    'gcp-staging': {
        'agent_url': 'https://leartech-automated-agent-jx-staging.jx.leartech.com',
    },
    'az-staging': {
        'agent_url': 'https://leartech-automated-agent-jx-staging.az.leartech.com',
    },
}

DEFAULT_CLUSTER = 'gcp-staging'


@dataclass(frozen=True)
class ClusterConfig:
    """One cluster's URL set. Frozen so callers can't mutate the file's view."""

    name: str
    agent_url: str

    def as_dict(self) -> dict[str, str]:
        return {'agent_url': self.agent_url}


@dataclass
class CliConfig:
    """Merged view: file ⊕ built-in defaults. Always non-empty."""

    default_cluster: str = DEFAULT_CLUSTER
    clusters: dict[str, ClusterConfig] = field(default_factory=dict)
    source_path: Path | None = None

    def resolve_cluster(self, name: str | None) -> ClusterConfig:
        """Resolve a cluster reference, falling back to default.

        Resolution order for an explicit ``name``:

        1. **Exact match** against a configured cluster name.
        2. **Prefix match** when exactly one configured name starts with
           ``name`` (e.g. ``'gcp'`` → ``'gcp-staging'``). This is the
           operator-friendly short form documented in the CLI help.
        3. Otherwise raise ``ValueError`` listing what's available, or —
           when the prefix matches more than one cluster — listing the
           ambiguous candidates so the operator can disambiguate.

        Never silently falls back to default on an unknown name — that
        would mask typos like ``--cluster gcp-prood``.
        """
        if name is None:
            name = self.default_cluster
        if name in self.clusters:
            return self.clusters[name]
        # Prefix match: gcp → gcp-staging when there's a single candidate.
        prefix_matches = sorted(n for n in self.clusters if n.startswith(name))
        if len(prefix_matches) == 1:
            return self.clusters[prefix_matches[0]]
        available = sorted(self.clusters)
        if len(prefix_matches) > 1:
            raise ValueError(
                f'Ambiguous cluster prefix {name!r}; matches {prefix_matches}. '
                f'Use the full name (one of: {prefix_matches}).'
            )
        raise ValueError(
            f'Unknown cluster {name!r}; available clusters: {available}. '
            f'Edit ~/.config/{APP_NAME}/{CONFIG_FILENAME} to add it.'
        )


def config_path() -> Path:
    """Path to the user's config file (may not exist yet)."""
    base = os.environ.get(DEFAULT_CONFIG_DIR_ENV)
    config_dir = Path(base) if base else Path.home() / '.config'
    return config_dir / APP_NAME / CONFIG_FILENAME


def _normalise_file_clusters(raw: dict[str, Any]) -> dict[str, ClusterConfig]:
    """Validate + lift the ``clusters:`` dict from the file into typed records.

    A stray legacy ``orch_url`` key inside a cluster body is silently
    dropped — the leartech-orchestrator service that owned it has been
    decommissioned, and rejecting the config just because an operator's
    file still names it would break their pipx install for no benefit.
    """
    out: dict[str, ClusterConfig] = {}
    clusters = raw.get('clusters') or {}
    if not isinstance(clusters, dict):
        raise ValueError(f'clusters: must be a mapping in {CONFIG_FILENAME}, got {type(clusters).__name__}')
    for name, body in clusters.items():
        if not isinstance(body, dict):
            raise ValueError(f'clusters.{name}: must be a mapping, got {type(body).__name__}')
        agent_url = body.get('agent_url')
        if not isinstance(agent_url, str):
            raise ValueError(f'clusters.{name}: `agent_url` is required (string).')
        # Legacy `orch_url` is tolerated + ignored — see module docstring.
        out[name] = ClusterConfig(name=name, agent_url=agent_url)
    return out


def load_config(path: Path | None = None) -> CliConfig:
    """Load the user's config, merged over built-in defaults.

    Built-ins seed the cluster map first so a fresh install (no file)
    still resolves ``gcp-staging`` / ``az-staging``. The file's entries
    override built-ins by key, so an operator can rebind
    ``gcp-staging.agent_url`` to a hotfix instance without dropping the
    other clusters.
    """
    target = path if path is not None else config_path()
    clusters = {name: ClusterConfig(name=name, agent_url=body['agent_url']) for name, body in _BUILTIN_CLUSTERS.items()}
    default_cluster = DEFAULT_CLUSTER
    source: Path | None = None
    if target.is_file():
        raw = yaml.safe_load(target.read_text(encoding='utf-8')) or {}
        if not isinstance(raw, dict):
            raise ValueError(f'{target}: expected a top-level mapping, got {type(raw).__name__}')
        file_clusters = _normalise_file_clusters(raw)
        clusters.update(file_clusters)
        # File-level default_cluster wins — operators routinely flip this
        # field when promoting from staging to prod.
        if 'default_cluster' in raw:
            requested = raw['default_cluster']
            if not isinstance(requested, str):
                raise ValueError('default_cluster: must be a string')
            if requested not in clusters:
                raise ValueError(f'default_cluster {requested!r} not in clusters map; available: {sorted(clusters)}')
            default_cluster = requested
        source = target
    return CliConfig(default_cluster=default_cluster, clusters=clusters, source_path=source)


def save_config(config: CliConfig, path: Path | None = None) -> Path:
    """Write ``config`` to disk (creating directories as needed).

    Only writes the *non-builtin* deltas so that an operator inspecting
    the file sees their own edits, not the entire built-in map. If the
    operator hasn't changed anything, the file is still written so
    subsequent ``config show`` reports a stable on-disk location.
    """
    target = path if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    overrides: dict[str, dict[str, str]] = {}
    for name, cluster in config.clusters.items():
        builtin = _BUILTIN_CLUSTERS.get(name)
        if builtin is None or builtin != cluster.as_dict():
            overrides[name] = cluster.as_dict()
    body: dict[str, Any] = {'default_cluster': config.default_cluster}
    if overrides:
        body['clusters'] = overrides
    target.write_text(yaml.safe_dump(body, sort_keys=True), encoding='utf-8')
    return target


def resolve_url(
    key: str,
    *,
    flag_value: str | None,
    cluster: str | None,
    config: CliConfig | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Resolve one URL key (``agent_url``) by priority chain.

    Order: ``--<key>-url`` flag → ``LEARTECH_<KEY>_URL`` env → config
    file (cluster lookup) → built-in default for the named cluster.

    ``env`` is exposed for tests; defaults to ``os.environ``.
    """
    if key != 'agent_url':
        raise ValueError(f'resolve_url: unknown key {key!r}')
    if flag_value:
        return flag_value
    env_map = env if env is not None else dict(os.environ)
    env_name = f'LEARTECH_{key.upper()}'
    if env_value := env_map.get(env_name):
        return env_value
    cfg = config if config is not None else load_config()
    resolved = cfg.resolve_cluster(cluster)
    # ``getattr`` returns ``Any``; the validated keyset above guarantees
    # the attribute exists + is ``str``-typed on ``ClusterConfig``.
    value: str = getattr(resolved, key)
    return value
