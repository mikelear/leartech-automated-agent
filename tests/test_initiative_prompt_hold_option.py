"""Unit tests for the hold-conditional initiative-agent prompt renderer.

Historically ``gate/agent/initiative_prompt.py`` hardcoded an unconditional
"always post `/hold` on a freshly-opened PR" instruction. That replicated Tide
(green→auto-merge) and prevented plans from self-completing. The current shape
treats hold as an OPT-IN Initiative field:

* ``hold=False`` (default) — no `/hold` posting; Tide auto-merges on green.
* ``hold=True`` — the agent posts `/hold` after opening the PR.

Regardless of the value, the agent must NEVER post `/hold cancel`.

These tests pin:

* Default rendering does NOT include a `/hold` posting instruction.
* ``hold=True`` rendering DOES include the `gh pr comment ... /hold`
  instruction under step 5.
* Both renderings keep the "never post `/hold cancel`" hard rule (only an
  approver cancels a hold placed by anyone else).
* The backward-compat ``INITIATIVE_SYSTEM_PROMPT`` constant equals the
  ``hold=False`` rendering (so existing imports keep working).
* The compose pipeline in ``gate/agent/initiative.py`` calls the renderer
  with ``initiative.hold`` so a YAML with ``hold: true`` reaches the prompt.
"""

from __future__ import annotations

from gate.agent.initiative_prompt import (
    INITIATIVE_SYSTEM_PROMPT,
    render_initiative_system_prompt,
)
from gate.initiatives.loader import Initiative

HOLD_POSTING_MARKER = 'gh pr comment <pr> -R <repo> --body "/hold"'
NEVER_CANCEL_MARKER = 'Never post `/hold cancel`'


def test_default_rendering_omits_hold_posting_instruction() -> None:
    """No `/hold` posting when the initiative doesn't opt in."""
    rendered = render_initiative_system_prompt(hold=False)
    assert HOLD_POSTING_MARKER not in rendered
    # It must actively state the default (Tide auto-merges on green) so the
    # agent knows the absence of a hold instruction is intentional, not an
    # oversight. Match ignoring whitespace since the prompt wraps across lines.
    normalized = ' '.join(rendered.split())
    assert 'let Tide auto-merge' in normalized
    assert 'hold: false' in rendered


def test_hold_true_rendering_includes_hold_posting_instruction() -> None:
    """`hold=True` renders the explicit `gh pr comment ... /hold` block."""
    rendered = render_initiative_system_prompt(hold=True)
    assert HOLD_POSTING_MARKER in rendered
    # And cites the initiative's opt-in as the reason, so the agent understands
    # this is scoped to this initiative rather than a global rule.
    assert 'hold: true' in rendered


def test_both_renderings_prohibit_hold_cancel() -> None:
    """The `/hold cancel` prohibition is UNIVERSAL — even when the agent isn't
    posting `/hold` itself, a human may have placed one that the agent must
    not cancel."""
    for hold in (False, True):
        rendered = render_initiative_system_prompt(hold=hold)
        assert NEVER_CANCEL_MARKER in rendered, f'`/hold cancel` prohibition missing for hold={hold}'


def test_hold_false_hard_rule_forbids_posting_hold() -> None:
    """The hard-rules block for the default must be explicit about NOT posting `/hold`.
    Silence would leave the door open for the agent to post it "just to be safe";
    a positive prohibition anchors the new default."""
    rendered = render_initiative_system_prompt(hold=False)
    assert 'Do NOT post `/hold`' in rendered


def test_hold_true_hard_rule_requires_posting_hold() -> None:
    """The hard-rules block for hold=True mirrors the step-5 instruction."""
    rendered = render_initiative_system_prompt(hold=True)
    assert 'Always post `/hold` on a freshly-opened PR' in rendered


def test_backward_compat_constant_matches_default_rendering() -> None:
    """``INITIATIVE_SYSTEM_PROMPT`` is preserved as a module-level constant
    for existing importers; it must equal the ``hold=False`` rendering."""
    assert INITIATIVE_SYSTEM_PROMPT == render_initiative_system_prompt(hold=False)


def test_step_5_opens_pr_via_mcp_tool_never_gh_create() -> None:
    """Step 5 opens the PR by calling the `open_pr` MCP tool (structured
    create + authoritative publish), NEVER `gh pr create` (which we used to
    scrape the number out of — the targetPR mis-capture bug). Only the
    hold=True variant adds the `/hold` posting. Pin both so a future edit that
    re-introduces `gh pr create` or an always-hold surfaces here."""
    hold_true = render_initiative_system_prompt(hold=True)
    hold_false = render_initiative_system_prompt(hold=False)
    # Both variants open via the open_pr MCP tool — and never `gh pr create`.
    assert '`open_pr` MCP tool' in hold_true
    assert '`open_pr` MCP tool' in hold_false
    assert 'gh pr create' not in hold_true
    assert 'gh pr create' not in hold_false
    # Only the hold=True variant posts the merge hold.
    assert HOLD_POSTING_MARKER in hold_true
    assert HOLD_POSTING_MARKER not in hold_false


def test_initiative_default_hold_flows_to_prompt() -> None:
    """A default-shaped Initiative (no `hold:` in YAML) must render the
    ``hold=False`` prompt. This pins the wiring between the loader and the
    prompt renderer at the source-of-truth level (the Initiative model).
    """
    init = Initiative(name='x', repo='r', branch='b', goal='g')
    assert init.hold is False
    rendered = render_initiative_system_prompt(hold=init.hold)
    assert HOLD_POSTING_MARKER not in rendered


def test_initiative_hold_true_flows_to_prompt() -> None:
    """`Initiative(hold=True)` renders the `/hold` posting instruction —
    same end-to-end wiring test but for the opt-in shape."""
    init = Initiative(name='x', repo='r', branch='b', goal='g', hold=True)
    assert init.hold is True
    rendered = render_initiative_system_prompt(hold=init.hold)
    assert HOLD_POSTING_MARKER in rendered


def test_initiative_compose_calls_renderer_with_hold() -> None:
    """Mirror the compose pipeline shape from ``gate/agent/initiative.py``.

    We can't call ``run_initiative`` in a unit test (it needs an
    ANTHROPIC_API_KEY + clones repos), so reconstruct the compose shape here.
    If ``initiative.py`` drifts from this shape, the real-runtime path would
    catch it — but pinning the intent surfaces regressions faster.
    """
    from gate.agent.calibrations import JX3_CALIBRATION_HEADER, load_jx3_calibration
    from gate.agent.lessons import render_for

    init = Initiative(name='x', repo='r', branch='b', goal='g', hold=True)
    blocks: list[str] = [load_jx3_calibration()]
    lessons = render_for('initiative_agent')
    if lessons:
        blocks.append(lessons)
    blocks.append(render_initiative_system_prompt(hold=init.hold))
    composed = '\n\n---\n\n'.join(blocks)

    # Calibration precedes the role prompt.
    assert JX3_CALIBRATION_HEADER in composed
    # And the /hold instruction is present because hold=True was threaded in.
    assert HOLD_POSTING_MARKER in composed
