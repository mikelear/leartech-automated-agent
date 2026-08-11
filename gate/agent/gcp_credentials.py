"""Bridge the projected GCS credential from env → file for every agent.

The orchestrator-controller projects the artifact-read service-account key as the
``GOOGLE_APPLICATION_CREDENTIALS_JSON`` env var — opt-in per AgentType.gcsSecretName,
the SAME secret→env way it passes ``GH_TOKEN`` / ``ANTHROPIC_API_KEY``. Those two
are read from the env NATIVELY by their tools (gh/git, the Claude SDK), so they
need no setup. GCP tooling is the exception: ``gcloud`` / ``gsutil`` /
``google-cloud-storage`` do NOT read a JSON-in-env — they want a FILE, discovered
via ``GOOGLE_APPLICATION_CREDENTIALS``. So we bridge env→file ONCE at agent
startup.

Without this, an agent told to read a ``gs://`` artifact falls back to workload
identity (scoped elsewhere → 403) and then silently proceeds from whatever the
prompt embedded inline — the exact failure that made the first artifact-driven
plan invent brand colours instead of reading them.

This lives in the shared runtime image and is invoked from the ``gate.agent``
package import, so EVERY Python agent entrypoint (initiative / ba / infra) gets it
— no per-agent or per-Plan wiring. No-op when the env is absent (most agents /
preview namespaces) or when ``GOOGLE_APPLICATION_CREDENTIALS`` is already set.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ENV_JSON = 'GOOGLE_APPLICATION_CREDENTIALS_JSON'
_ENV_FILE = 'GOOGLE_APPLICATION_CREDENTIALS'


def materialize_gcp_credentials() -> str | None:
    """Write ``GOOGLE_APPLICATION_CREDENTIALS_JSON`` (if projected) to a 0600 file
    and export ``GOOGLE_APPLICATION_CREDENTIALS`` at it. Returns the file path when
    written, else ``None`` (env absent, or a file path already configured).

    Idempotent: safe to call more than once; a second call is a no-op because
    ``GOOGLE_APPLICATION_CREDENTIALS`` is now set.
    """
    raw = os.environ.get(_ENV_JSON, '').strip()
    if not raw:
        return None
    if os.environ.get(_ENV_FILE):
        return None
    path = Path(tempfile.gettempdir()) / 'gcp-credentials.json'
    path.write_text(raw)
    path.chmod(0o600)
    os.environ[_ENV_FILE] = str(path)
    return str(path)
