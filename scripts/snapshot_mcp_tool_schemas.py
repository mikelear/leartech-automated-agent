"""Discover live MCP tool schemas and write ``docs/mcp-tool-schemas.json``.

The prompt tells the agent to call MCP tools by NAME with specific arguments.
Those signatures — tool name, argument names, argument types — are a
contract with the servers in ``leartech-mcp-servers``. When the prompt drifts
from the schema the agent burns turns discovering the mismatch at run time
(the ``pr_number`` string-vs-integer bug hit three times across two runs).

This script mirrors ``scripts/render_system_prompt.py``:

  * ``--write`` — connect to the live MCP hosts, list every tool on the three
    servers the initiative agent uses (``jx3_flow``, ``pr_context``,
    ``tekton``), and write the schemas to a committed JSON artefact.
  * bare run — connect to the live hosts and confirm the artefact matches;
    exit non-zero if it is stale.

The tests then assert the prompt matches the ARTEFACT (offline). A stale
artefact is caught here by an operator; a mismatch between prompt and
artefact is caught by ``tests/test_prompt_contract.py`` in CI.

Both modes require live MCP credentials (``LEARTECH_AUTH_*`` env), so a run
without them fails loudly rather than reporting success while checking
nothing — the failure mode this whole contract exists to remove.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from gate.mcp_servers.remote import discover_mounts, mint_mcp_token

OUTPUT = pathlib.Path('docs/mcp-tool-schemas.json')

# The MCP servers the INITIATIVE agent actually calls tools on. Widening this
# set is a deliberate choice — see the review agent's MCP list in
# ``gate/agent/mcp_catalog.yaml`` if you want the review-role tools too.
INITIATIVE_MCPS = ('jx3_flow', 'pr_context', 'tekton')

# Server-name → agent-facing MCP name. Must match ``_agent_mcp_name`` in
# ``gate/mcp_servers/remote.py`` so the artefact's keys line up with the
# ``mcp__<agent-name>__<tool>`` names the prompt references.
_AGENT_NAME_PREFIX = 'leartech-'


def _agent_mcp_name(server: str) -> str:
    return _AGENT_NAME_PREFIX + server.replace('_', '-')


async def _discover_tools_for(base: str, token: str, path: str) -> dict[str, object]:
    """Return ``{tool_name: {description, inputSchema}}`` for one MCP mount."""
    url = f'{base}{path}'
    headers = {'Authorization': f'Bearer {token}'}
    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listing = await session.list_tools()
    return {
        tool.name: {
            'description': tool.description,
            'inputSchema': tool.inputSchema,
        }
        for tool in listing.tools
    }


async def discover() -> dict[str, dict[str, object]]:
    """Connect to the live MCP hosts and return the initiative-agent schemas.

    Raises ``RuntimeError`` when the env is not configured — the caller then
    prints a diagnostic and exits non-zero. The whole point of this script
    is to REFUSE to succeed silently when it cannot check anything.
    """
    base = os.environ.get('LEARTECH_MCP_URL', '').rstrip('/')
    if not base:
        raise RuntimeError(
            'LEARTECH_MCP_URL is unset — cannot discover tool schemas. '
            'Run this inside the cluster or with the env pointed at a reachable MCP host.'
        )
    token = mint_mcp_token()
    if not token:
        raise RuntimeError(
            'could not mint an aud=leartech-mcp token — check LEARTECH_AUTH_TOKEN_URL / '
            'LEARTECH_AUTH_CLIENT_ID / LEARTECH_AUTH_CLIENT_SECRET / LEARTECH_AUTH_SCOPE.'
        )
    mounts = discover_mounts(base, token)
    if mounts is None:
        raise RuntimeError(f'{base}/mcps discovery failed — cannot list tools.')
    missing = [name for name in INITIATIVE_MCPS if name not in mounts]
    if missing:
        advertised = ', '.join(sorted(mounts))
        raise RuntimeError(
            f'{base}/mcps does not advertise: {", ".join(missing)}. Advertised: {advertised}. '
            'Update INITIATIVE_MCPS or investigate the host.'
        )
    result: dict[str, dict[str, object]] = {}
    for server in INITIATIVE_MCPS:
        try:
            tools = await _discover_tools_for(base, token, mounts[server])
        except httpx.HTTPError as exc:
            raise RuntimeError(f'discovery for {server!r} failed: {exc}') from exc
        result[_agent_mcp_name(server)] = tools
    return result


def render(schemas: dict[str, dict[str, object]]) -> str:
    """JSON-serialise ``schemas`` with a trailing newline (POSIX-style).

    Sorted keys + two-space indent so the committed file diffs cleanly when a
    schema changes. Keeping the format deterministic is what lets bare-run
    freshness detection work: a byte-for-byte compare over the file.
    """
    return json.dumps(schemas, indent=2, sort_keys=True) + '\n'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--write', action='store_true', help='write the file instead of checking it')
    args = ap.parse_args()

    try:
        schemas = asyncio.run(discover())
    except RuntimeError as exc:
        print(f'discovery failed: {exc}', file=sys.stderr)
        return 2

    rendered = render(schemas)
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered)
        print(f'wrote {OUTPUT} ({len(rendered)} chars)')
        return 0
    if not OUTPUT.exists() or OUTPUT.read_text() != rendered:
        print(f'{OUTPUT} is stale — run: python scripts/snapshot_mcp_tool_schemas.py --write', file=sys.stderr)
        return 1
    print(f'{OUTPUT} is current')
    return 0


if __name__ == '__main__':
    sys.exit(main())
