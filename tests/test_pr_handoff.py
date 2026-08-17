"""The agent's per-turn PR checkpoint.

An iteration can be killed mid-thought (controller deadline, node move), and in
the AgentRun runtime turn count and spend live nowhere durable: no DB DSN is
injected, so run_driver's fallback is a process-local dict that dies with the
pod. These tests pin the cadence rule and the never-break-the-run contract.
"""

from __future__ import annotations

import pytest

from gate.agent import pr_handoff


class TestCadence:
    def test_first_turns_do_not_post_every_turn(self):
        assert not pr_handoff.should_post(turns=1, max_turns=300, last_posted_turn=0)
        assert not pr_handoff.should_post(turns=9, max_turns=300, last_posted_turn=0)

    def test_posts_on_the_interval(self):
        assert pr_handoff.should_post(turns=10, max_turns=300, last_posted_turn=0)
        assert pr_handoff.should_post(turns=20, max_turns=300, last_posted_turn=10)

    def test_never_posts_twice_for_the_same_turn(self):
        assert not pr_handoff.should_post(turns=10, max_turns=300, last_posted_turn=10)

    def test_posts_every_turn_near_the_ceiling(self):
        # 296/300 → 4 left: every turn matters here, this is the window where an
        # interruption is most likely and the successor's need is greatest.
        assert pr_handoff.should_post(turns=296, max_turns=300, last_posted_turn=295)
        assert pr_handoff.should_post(turns=297, max_turns=300, last_posted_turn=296)

    def test_small_ceiling_uses_the_absolute_floor(self):
        # 10% of 20 is 2 turns, which is no warning at all — the floor takes over.
        assert pr_handoff.is_near_limit(8, 20)

    def test_no_ceiling_means_interval_only(self):
        assert not pr_handoff.is_near_limit(9999, 0)
        assert pr_handoff.should_post(turns=10, max_turns=0, last_posted_turn=0)

    def test_turn_zero_never_posts(self):
        assert not pr_handoff.should_post(turns=0, max_turns=300, last_posted_turn=0)


class TestSummary:
    def test_carries_position_and_last_tool(self):
        s = pr_handoff.build_summary(turns=42, max_turns=300, last_tool_call='pr_gate_snapshot', iteration=2)
        assert 'turn 42/300' in s
        assert 'iteration 2' in s
        assert 'pr_gate_snapshot' in s

    def test_warns_when_near_the_ceiling(self):
        s = pr_handoff.build_summary(turns=298, max_turns=300, last_tool_call=None, iteration=1)
        assert 'budget left' in s
        assert 'take over' in s

    def test_tolerates_unknown_ceiling_and_no_tool(self):
        s = pr_handoff.build_summary(turns=5, max_turns=0, last_tool_call=None, iteration=0)
        assert 'turn 5' in s
        assert '/0' not in s


class TestPostHandoff:
    @staticmethod
    def _caller(captured: list, result=None, err=None):
        async def call(base_url, server, tool, args):
            captured.append((base_url, server, tool, args))
            return (result or {'action': 'created'}), err

        return call

    @pytest.mark.asyncio
    async def test_sends_only_what_the_agent_alone_knows(self):
        captured: list = []
        ok, err = await pr_handoff.post_handoff(
            base_url='http://mcp',
            repo='mikelear/leartech-mcp-servers',
            pr_number=78,
            run_id='agentrun-x',
            iteration=2,
            turns=287,
            max_turns=300,
            cost_usd=4.10,
            last_tool_call='wait_for_terminal',
            model='claude-opus-4-8',
            tool_caller=self._caller(captured),
        )
        assert ok and err is None
        _, server, tool, args = captured[0]
        assert (server, tool) == ('jx3_flow', 'post_pr_handoff')
        assert args['turns_used'] == 287
        assert args['turns_max'] == 300
        assert args['cost_usd'] == 4.10
        assert args['model'] == 'claude-opus-4-8'
        assert args['run_id'] == 'agentrun-x'
        # budget_state is deliberately NOT sent: the MCP server derives it, so
        # there is one implementation of that rule rather than two.
        assert 'budget_state' not in args

    @pytest.mark.asyncio
    async def test_skips_when_no_pr_exists_yet(self):
        captured: list = []
        ok, err = await pr_handoff.post_handoff(
            base_url='http://mcp',
            repo='owner/repo',
            pr_number=0,
            run_id='r',
            iteration=1,
            turns=5,
            max_turns=300,
            cost_usd=None,
            last_tool_call=None,
            tool_caller=self._caller(captured),
        )
        assert not ok
        assert err == 'no PR yet'
        assert captured == [], 'must not call the tool before a PR exists'

    @pytest.mark.asyncio
    async def test_tool_error_is_reported_not_raised(self):
        ok, err = await pr_handoff.post_handoff(
            base_url='http://mcp',
            repo='o/r',
            pr_number=1,
            run_id='r',
            iteration=1,
            turns=10,
            max_turns=300,
            cost_usd=1.0,
            last_tool_call=None,
            tool_caller=self._caller([], err='MCP discovery failed'),
        )
        assert not ok
        assert 'discovery failed' in err

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self):
        async def boom(*_a, **_k):
            raise RuntimeError('connection reset')

        ok, err = await pr_handoff.post_handoff(
            base_url='http://mcp',
            repo='o/r',
            pr_number=1,
            run_id='r',
            iteration=1,
            turns=10,
            max_turns=300,
            cost_usd=1.0,
            last_tool_call=None,
            tool_caller=boom,
        )
        assert not ok
        assert 'RuntimeError' in err, 'a checkpoint failure must never break the run'
