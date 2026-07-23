"""Tests for gate.agent.infra_agent — the infra-agent entrypoint wiring (local CoS).

Cluster CoS (deferred): the infra agent runs the repo-factory Plan end-to-end and a
hello-world service reaches /health=200 on both clusters. Here we prove the entrypoint is
wired: write-mode + MCP tools granted, the deterministic-scaffold rule is in the prompt,
the infra_agent role exists in the catalog and references only real MCPs.
"""

from __future__ import annotations

from gate.agent import infra_agent
from gate.agent.mcp_catalog import load_catalog


def test_infra_role_in_catalog_references_real_mcps() -> None:
    catalog = load_catalog()
    assert 'infra_agent' in catalog.roles
    role = catalog.roles['infra_agent']
    assert {'Read', 'Write', 'Edit', 'Bash'} <= set(role.tools)
    for mcp in role.mcps:
        assert mcp in catalog.mcp_servers, f'infra_agent references unknown MCP {mcp!r}'


def test_allowed_tools_grant_write_and_open_pr() -> None:
    tools = infra_agent.INFRA_ALLOWED_TOOLS
    assert {'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep'} <= set(tools)
    assert 'mcp__leartech-pr-context__open_pr' in tools  # opens PRs via the structured tool


def test_system_prompt_enforces_deterministic_scaffold_and_two_clusters() -> None:
    prompt = infra_agent._build_system_prompt()
    assert 'repo_factory' in prompt  # scaffolding goes through the deterministic tool
    assert 'never hand-edit' in prompt or 'never hand-edit or grep' in prompt
    assert 'jx-build-cluster-gsm' in prompt and 'jx-build-cluster-akv' in prompt
    assert 'release-health-check' in prompt  # the merged!=healthy verification action


def test_task_prompt_embeds_action_and_inputs() -> None:
    out = infra_agent._task_prompt('create-repo', {'newRepo': 'mikelear/hello-go'})
    assert 'create-repo' in out
    assert 'mikelear/hello-go' in out
