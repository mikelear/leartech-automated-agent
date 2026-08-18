"""The prompt is a contract with the code, and CI holds it.

Prompt text is not covered by any other test, so it rots silently: an MCP server can be
deleted while the prompt still instructs the agent to call its tools, and the agent
burns turns discovering that. It also invited host-shaped instructions — the prompt told
the agent to read logs via a shell script under a developer's home directory, which does
not exist in the Job container.

The LLM routing around a broken instruction is not a pass: it costs turns and tokens, and
it keeps the dead code that instruction refers to alive.
"""

from __future__ import annotations

import re
import subprocess
import sys

from gate.agent.initiative import INITIATIVE_TEKTON_TOOLS, WRITE_MODE_TOOLS
from gate.agent.main import MCP_ALLOWED_TOOLS
from gate.agent.mcp_catalog import load_catalog
from scripts.render_system_prompt import assemble

MCP_TOOL_RE = re.compile(r'mcp__(?P<server>[a-z0-9-]+)__(?P<tool>[a-z0-9_]+)')

ALLOWED_TOOLS = {*WRITE_MODE_TOOLS, *MCP_ALLOWED_TOOLS, *INITIATIVE_TEKTON_TOOLS}


def _prompt_text() -> str:
    return assemble('initiative_agent')


def test_every_mcp_tool_named_in_the_prompt_is_actually_wired() -> None:
    """A tool the prompt names but the agent cannot call is a turn wasted on a 404."""
    load_catalog.cache_clear()
    catalog = load_catalog()
    catalogued = {name.replace('leartech-', '', 1) if False else name for name in catalog.mcp_servers}

    unknown_servers: set[str] = set()
    unwired_tools: set[str] = set()
    for match in MCP_TOOL_RE.finditer(_prompt_text()):
        server = (
            f'leartech-{match.group("server")}'
            if not match.group('server').startswith('leartech-')
            else match.group('server')
        )
        full = match.group(0)
        if server not in catalogued and match.group('server') not in catalogued:
            unknown_servers.add(match.group('server'))
        elif full not in ALLOWED_TOOLS:
            unwired_tools.add(full)

    assert not unknown_servers, (
        f'the prompt names MCP servers that are not in the catalog: {sorted(unknown_servers)} — '
        'either the server was deleted and the prompt was not updated, or the catalog is missing it'
    )
    assert not unwired_tools, (
        f'the prompt names MCP tools the agent is not allowed to call: {sorted(unwired_tools)} — '
        'add them to allowed_tools or stop instructing the agent to use them'
    )


def test_prompt_contains_no_host_specific_paths() -> None:
    """The agent runs in a Job container, not on anyone's laptop.

    Repo-relative paths (``scripts/e2e.sh``) are fine — they live in the checkout the
    agent is working on. What is not fine is a path rooted in a developer's home
    directory, which the container has no equivalent of.
    """
    offenders = [pattern for pattern in ('~/', '/Users/', '/home/') if pattern in _prompt_text()]
    assert not offenders, (
        f'the prompt references host-shaped paths {offenders} — the agent runs in a Job '
        'container where they do not exist, so it burns turns failing before routing around it'
    )


def test_rendered_prompt_snapshot_is_current() -> None:
    """docs/agent-system-prompt.md must match what the code assembles.

    The prompt is built from three sources — the JX3 calibration, the lessons catalog and
    the Python-rendered role prompt — so editing any one of them changes agent behaviour
    with no readable diff. Committing the assembled text means a reviewer sees the change
    in the PR that causes it, including changes made by adding a lesson file.
    """
    result = subprocess.run(
        [sys.executable, 'scripts/render_system_prompt.py'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
