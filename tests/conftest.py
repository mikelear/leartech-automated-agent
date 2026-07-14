"""Session-wide pytest fixtures + auth-hardening C1 opt-out.

Auth-hardening C1 (2026-07): production defaults are now fail-closed —
``LEARTECH_AUTH_REQUIRED`` defaults to ``true`` and the middleware raises
at startup when issuer/audience are unset. Every cluster (staging + preview)
inherits the chart's issuer/audience defaults so the pod boots 1/1.

Local pytest runs (this suite) don't have a real Hydra to hit, so we set
``LEARTECH_AUTH_REQUIRED=false`` at collection time. This runs BEFORE any
test module is imported, so ``from app.main import app`` at module scope
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

# Applied AT IMPORT TIME so it lands before pytest starts collecting tests
# (and therefore before any test module runs `from app.main import app`,
# which triggers the middleware install).
os.environ.setdefault('LEARTECH_AUTH_REQUIRED', 'false')
