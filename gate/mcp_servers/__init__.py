"""MCP server wrappers — the remaining in-process shims plus the remote-MCP wiring.

Each in-process builder returns an ``McpSdkServerConfig`` ready to pass into
``ClaudeAgentOptions(mcp_servers=...)``. The remote builder
(:func:`build_remote_mcp_servers`) discovers deployed Go MCP servers from
``leartech-mcp-servers`` and wires each one behind :mod:`gate.mcp_servers.stdio_bridge`
so tools flow over standard client-side MCP — the same shape non-Anthropic
runtimes will be able to consume (see ``AI-GATEWAY-AND-PORTABILITY.md``).

The migration direction is **Go-first**: every tool the deployed Go platform
already covers is retired from this repo and consumed remotely; only the tools
that can't move remote (or that don't yet have a Go equivalent) still live here.

Migration snapshot (bucket map — see `feat/shims-to-real-mcp` PR for the full table):

- **DUPLICATE / migrated already** (remote-only via :mod:`.remote`):

  * ``pr_context`` — ``open_pr``, ``get_pr_metadata``, ``get_pr_diff``,
    ``list_changed_files``. Was in-process ``gate.mcp_servers.pr_context_server``;
    now served by the Go ``leartech-mcp-servers/pr_context`` deployment.
  * ``tekton`` — step-aware PipelineRun inspection (``list_pipelineruns_for_pr``,
    ``step_status``, ``step_logs``, ``cancel_pipelinerun``,
    ``cancel_superseded_for_pr``, ``wait_first_failure``). Was in-process
    ``gate.mcp_servers.tekton``; now the Go ``leartech-mcp-servers/tekton`` deployment.
  * ``jx3_flow`` — aggregate PR-check status (``list_pr_checks``,
    ``wait_for_terminal``, ``wait_for_first_failure_or_all_pass``). Was in-process
    ``gate.mcp_servers.pipeline_server``; now the Go ``leartech-mcp-servers/jx3_flow``
    deployment.
  * ``agent_api`` — ``fire_initiative`` (raw YAML body), ``get_catalog_entry``,
    ``list_catalog_entries``, ``list_initiative_runs``, ``get_initiative_run``,
    ``cancel_initiative_run``, ``send_command_to_run``, plus ``amend_plan`` /
    ``create_plan`` for BA. Retires the in-process
    ``gate.mcp_servers.initiatives_server`` shim (``fire_initiative`` +
    ``fire_initiative_inline``). By-name firing composes as ``get_catalog_entry
    → fire_initiative(initiative_body=…)`` client-side.

- **GAP / no Go MCP yet** (still in-process; DO NOT hand-migrate — build the Go
  equivalent in ``leartech-mcp-servers`` first):

  * :mod:`.artifacts_server` — ``list_playwright_runs`` (parses end2end-ui
    sticky comments) and ``head_artifact`` (HTTP HEAD to public GCS URLs).
    Should move to a Go MCP once one exists.
  * :mod:`.ai_gateway_web_server` — ``web_search`` / ``web_fetch`` via
    ``leartech-ai-gateway``. Not currently exposed as an ``leartech-mcp-servers``
    server; the wrapper is essentially an httpx client + JSON schema, so it
    stays until a Go equivalent lands.

- **KEEP-LOCAL / correctly in-process** (depend on agent-pod state — network
  boundary would be worse):

  * :mod:`.agent_local` — ``classify_step_failure`` (LLM-adjacent heuristic
    tables imported from :mod:`gate.agent.step_failure_diagnosis`) and
    ``rebase_branch_on_base`` (git ops on the cloned consumer-repo workspace).
  * :mod:`.criteria_server` — ``list_criteria`` / ``run_criteria_set``. Runs
    ``uv run pytest`` against ``gate/criteria/`` in THIS repo's checkout; the
    criteria code + the pytest process both live in the agent pod's workspace,
    so moving this remote would require projecting the workspace over a network
    filesystem (much worse boundary).

Adding a new remote MCP: add its host-side server name to
``WANTED_MCP_SERVERS`` in :mod:`.remote` and (if a role needs it) reference the
agent-facing name in that role's ``mcps`` list in
``gate/agent/mcp_catalog.yaml``. Nothing else needed on this side.
"""

from gate.mcp_servers.agent_local import build_agent_local_server
from gate.mcp_servers.ai_gateway_web_server import build_ai_gateway_web_server
from gate.mcp_servers.artifacts_server import build_artifacts_server
from gate.mcp_servers.criteria_server import build_criteria_server
from gate.mcp_servers.remote import build_remote_mcp_servers

__all__ = [
    'build_agent_local_server',
    'build_ai_gateway_web_server',
    'build_artifacts_server',
    'build_criteria_server',
    'build_remote_mcp_servers',
]
