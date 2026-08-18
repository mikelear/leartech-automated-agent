"""Session-wide pytest fixtures + auth-hardening C1 opt-out.

Auth-hardening C1 (2026-07): production defaults are now fail-closed —
``LEARTECH_AUTH_REQUIRED`` defaults to ``true`` and the middleware raises
at startup when issuer/audience are unset. Every cluster (staging + preview)
inherits the chart's issuer/audience defaults so the pod boots 1/1.

Local pytest runs (this suite) don't have a real Hydra to hit, so we set
``LEARTECH_AUTH_REQUIRED=false`` at collection time. This runs BEFORE any
test module is imported, so module-scope imports
still lands the middleware in optional mode. Suites that explicitly
exercise the required-mode branches (``tests/test_auth_middleware.py``)
construct their own :class:`AuthSettings` locally and are unaffected.

``os.environ.setdefault`` — not ``os.environ[...] = ...`` — so a developer
running ``LEARTECH_AUTH_REQUIRED=true pytest`` locally with a real Hydra
in scope still gets required-mode. The default is the safety net, not a
lockout.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault('LEARTECH_AUTH_REQUIRED', 'false')


_AGENTRUN_IDENTITY_ENV_VARS = (
    'AGENT_RUN_NAME',
    'AGENT_RUN_NAMESPACE',
    'LEARTECH_AGENTRUN_STATUS',
    'LEARTECH_RUN_ID',
)


@pytest.fixture(autouse=True)
def _reset_identity_snapshot(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset :mod:`gate.identity` module state AND scrub identity env before each test.

    Added by the sanitise-subprocess-identity initiative. Two side-effects:

    1. **Snapshot reset.** The identity module caches a captured snapshot
       from ``os.environ`` at
       :func:`gate.agent.initiative.run_initiative` entry and strips those
       vars from the env. That's process-scoped state: if a first test
       runs ``run_initiative``, it captures identity + strips env; a later
       test that ``monkeypatch.setenv`` the identity vars would then find
       its reads still hitting the stale FIRST test's snapshot instead of
       the freshly-set env. Resetting between tests keeps the
       accessor-fallback-to-``os.environ`` contract intact: any test that
       hasn't explicitly captured sees its ``monkeypatch`` env directly.

    2. **Identity env scrub.** When this pytest runs INSIDE a
       leartech-automated-agent pod (self-referential — the exact incident
       shape this initiative fixes), the pod env carries live
       ``AGENT_RUN_NAME`` / ``AGENT_RUN_NAMESPACE`` / etc. A test that
       accidentally hits ``_backstop_target_pr`` under those live vars would
       patch the AgentRun hosting the pytest — the 12:48:43 incident.
       Delenv-by-default here means every test starts with a clean slate;
       tests that legitimately need identity set (e.g.
       :file:`tests/test_initiative_targetpr_backstop.py`) monkeypatch.setenv
       within the test itself, which overrides this scrub for their duration.
    """
    from gate import identity

    identity.reset_for_tests()
    for _var in _AGENTRUN_IDENTITY_ENV_VARS:
        monkeypatch.delenv(_var, raising=False)
    try:
        yield
    finally:
        identity.reset_for_tests()
