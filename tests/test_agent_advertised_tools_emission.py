"""Wiring tests: every agent entrypoint that builds ClaudeAgentOptions MUST also
emit the `agent_advertised_tools` record at run start.

Why this file exists — the initiative brief: "a reader with only Loki and one
run id must be able to list the advertised MCP servers and the allowed tool
names". If any of the four entrypoints (initiative, ba_agent, infra_agent,
main/review) drops the call, that reader loses the ability to compute the
never-called set for THAT class of run — and none of the individual entrypoint
suites would catch it. This file pins the wire-up centrally so a regression is
loud.

Each test stubs the SDK ``query`` generator so no LLM is hit; the assertion is
solely that a single `agent_advertised_tools` record surfaces per run start,
carrying the servers + tools the options struct was built with.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from gate.agent import ba_agent, infra_agent, initiative, tool_logging
from gate.agent import main as review_main


@pytest.fixture
def cap_obslog() -> Iterator[io.StringIO]:
    """Capture obslog's JSON lines via a StringIO handler on its logger."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter('%(message)s'))
    lg = logging.getLogger('leartech.obslog')
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        lg.removeHandler(handler)


def _advertised_records(buf: io.StringIO) -> list[dict]:
    recs = [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]
    return [r for r in recs if r.get('event') == tool_logging.ADVERTISED_TOOLS_EVENT]


async def _one_success_result() -> Iterator[ResultMessage]:
    """Async generator stub: yield a single successful ResultMessage."""
    yield ResultMessage(
        subtype='success',
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id='sess',
        total_cost_usd=0.0,
        usage={},
        result=None,
    )


def _fake_query(*_args: object, **_kwargs: object):  # noqa: ANN202 — test stub async generator
    return _one_success_result()


def test_ba_agent_emits_advertised_tools_once_at_run_start(
    monkeypatch: pytest.MonkeyPatch, cap_obslog: io.StringIO
) -> None:
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    monkeypatch.setattr(ba_agent, 'query', _fake_query)
    brief = ba_agent.Brief.model_validate(
        {
            'name': 'x',
            'goal': 'g',
            'successCriteria': ['c'],
        }
    )
    rc = asyncio.run(ba_agent.run_ba_task(brief))
    assert rc == 0
    recs = _advertised_records(cap_obslog)
    assert len(recs) == 1, f'expected exactly one advertised-tools record, got {len(recs)}'
    rec = recs[0]
    # Allowed tools MUST be a superset of the BA agent's advertised allow-list.
    assert set(ba_agent.BA_ALLOWED_TOOLS) <= set(rec['allowed_tools'])
    # INFO-level (survives the deployed verbosity floor).
    assert rec['level'] == 'INFO'


def test_infra_agent_emits_advertised_tools_once_at_run_start(
    monkeypatch: pytest.MonkeyPatch, cap_obslog: io.StringIO
) -> None:
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    monkeypatch.setattr(infra_agent, 'query', _fake_query)
    rc = asyncio.run(infra_agent.run_infra_task('create-repo', {'name': 'x'}))
    assert rc == 0
    recs = _advertised_records(cap_obslog)
    assert len(recs) == 1
    assert set(infra_agent.INFRA_ALLOWED_TOOLS) <= set(recs[0]['allowed_tools'])
    assert recs[0]['level'] == 'INFO'


def test_review_agent_emits_advertised_tools_once_at_run_start(
    monkeypatch: pytest.MonkeyPatch, cap_obslog: io.StringIO
) -> None:
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    monkeypatch.setattr(review_main, 'query', _fake_query)
    rc = asyncio.run(review_main.review_pr('mikelear/leartech-automated-agent', 1))
    assert rc == 0
    recs = _advertised_records(cap_obslog)
    assert len(recs) == 1
    assert set(review_main.MCP_ALLOWED_TOOLS) <= set(recs[0]['allowed_tools'])
    assert recs[0]['level'] == 'INFO'


def test_initiative_emits_advertised_tools_once_at_run_start(
    monkeypatch: pytest.MonkeyPatch, cap_obslog: io.StringIO, tmp_path: Path
) -> None:
    """The initiative entrypoint is more elaborate (it clones + calls the SDK),
    so we bypass the clone by pointing repo_root at a pre-created cwd and by
    stubbing the SDK generator + the resume-detection primitives."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')

    # Provide a fake pre-existing repo root so the clone path is skipped.
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()

    # Neutralise every helper `run_initiative` calls that shells out to the
    # real `gh` / `git` CLI. In CI the `pr` pipeline runs on `python:3.13-slim`
    # which has NEITHER `gh` NOR `git` on PATH — those subprocess calls raise
    # FileNotFoundError, which is fatal to `run_initiative` even though our
    # unit under test (the `agent_advertised_tools` emission) has already run
    # at that point. Stubbing them out keeps the test focused on run-start.
    monkeypatch.setattr(
        initiative,
        '_detect_resume_context',
        lambda *, qualified_repo, branch: initiative.ResumeContext(
            is_resume=False, pr_number=None, branch_exists_on_remote=False
        ),
    )

    async def _stub_resolve_target_pr(qualified_repo: str, branch: str) -> None:
        return None

    monkeypatch.setattr(initiative, '_resolve_target_pr', _stub_resolve_target_pr)

    async def _stub_backstop_target_pr(*, qualified_repo: str, branch: str, pr_number: int | None) -> None:
        return None

    monkeypatch.setattr(initiative, '_backstop_target_pr', _stub_backstop_target_pr)
    monkeypatch.setattr(initiative, 'query', _fake_query)

    # Write a minimal initiative YAML the loader accepts.
    init_yaml = tmp_path / 'init.yaml'
    init_yaml.write_text(
        'name: log-advertised-tools-test\ngoal: dummy\nrepo: mikelear/leartech-automated-agent\nbranch: test-branch\n'
    )

    summary = asyncio.run(initiative.run_initiative(init_yaml, repo_root=repo_root, model='m', max_turns=1))
    # We don't assert on exit_code here — the initiative post-processing may
    # flag missing PR etc.; the only guarantee we care about is the advertised
    # emission fires exactly once at run start.
    _ = summary
    recs = _advertised_records(cap_obslog)
    assert len(recs) == 1, f'expected exactly one advertised-tools record, got {len(recs)}'
    tools = set(recs[0]['allowed_tools'])
    # The initiative wires Write/Edit/Bash (write-mode) — a superset check pins
    # the fact that this is the WRITE-mode entrypoint's advertised set.
    assert {'Read', 'Write', 'Edit', 'Bash'} <= tools
    assert recs[0]['level'] == 'INFO'


def test_advertised_tools_record_carries_run_id_across_entrypoints(
    monkeypatch: pytest.MonkeyPatch, cap_obslog: io.StringIO
) -> None:
    """A single check that the ambient-context join key survives: the run id
    must be stamped on the record no matter which entrypoint emitted it."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    monkeypatch.setenv('LEARTECH_RUN_ID', 's0-run-y')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
    monkeypatch.setattr(ba_agent, 'query', _fake_query)
    brief = ba_agent.Brief.model_validate({'name': 'x', 'goal': 'g', 'successCriteria': ['c']})
    asyncio.run(ba_agent.run_ba_task(brief))
    (rec,) = _advertised_records(cap_obslog)
    assert rec['run_id'] == 's0-run-y'
    assert rec['namespace'] == 'jx-staging'
