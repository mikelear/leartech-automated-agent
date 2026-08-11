"""Agent SDK loop wrapping the gate.

Reads a PR via the MCP servers (`gate.mcp_servers`), drives Claude through review,
returns the verdict. Read-only in v1; write-driven initiative loop comes next.
"""

# Runtime bootstrap (runs on package import — the shared hook across every Python
# agent entrypoint: initiative / ba / infra). Bridges the controller-projected
# GOOGLE_APPLICATION_CREDENTIALS_JSON env → a file + GOOGLE_APPLICATION_CREDENTIALS
# so gcloud/gsutil/GCS libs authenticate for ALL agents, not just dev ones and not
# via each Plan's prompt. Guarded no-op when the env isn't projected. See
# gate.agent.gcp_credentials for why GCS needs this bridge while GH_TOKEN /
# ANTHROPIC_API_KEY (read from env natively) do not.
from gate.agent.gcp_credentials import materialize_gcp_credentials

materialize_gcp_credentials()

from gate.agent.initiative import run_initiative  # noqa: E402  (after the credential bootstrap, intentionally)
from gate.agent.main import review_pr  # noqa: E402

__all__ = ['materialize_gcp_credentials', 'review_pr', 'run_initiative']
