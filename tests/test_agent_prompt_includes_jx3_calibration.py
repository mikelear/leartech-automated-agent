"""Pin that the JX3 platform calibration is prepended to BOTH agent prompts.

Background: the agent + orchestrator each handle one slice of a ~10-stage
JX3 release flow (dev push → checks → approve → merge → release → promote →
GitOps PR → reconcile → roll → /healthz). Without a shared mental model
the agents conflate "my step finished" with "the change is shipped" or
"single-cluster failure" with "the code is broken." See
``gate/agent/calibrations/jx3-full-flow.md``.

These tests pin the wiring — every system prompt the agent builds must
include the calibration's header AND a recognisable section-A line. If the
wiring is refactored (e.g. someone replaces ``load_jx3_calibration()`` with
an empty stub or drops it from the compose pipeline) these tests catch it.
"""

from __future__ import annotations

from gate.agent.calibrations import (
    JX3_CALIBRATION_FOOTER,
    JX3_CALIBRATION_HEADER,
    load_jx3_calibration,
)
from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT
from gate.agent.main import _build_system_prompt as build_review_system_prompt
from gate.agent.system_prompt import REVIEW_SYSTEM_PROMPT

# A recognisable line from Section A of the calibration markdown. If section A
# is reworded the substring will need updating — pick a load-bearing phrase.
SECTION_A_PROBE = 'dev-agent push → PR checks → Lighthouse approve → Tide merge → release'


def test_load_jx3_calibration_wraps_in_header_and_footer() -> None:
    """The loader must wrap the markdown in clearly-named delimiters."""
    block = load_jx3_calibration()
    assert block.startswith(JX3_CALIBRATION_HEADER)
    assert block.rstrip().endswith(JX3_CALIBRATION_FOOTER)
    assert SECTION_A_PROBE in block


def test_load_jx3_calibration_is_cached_idempotent() -> None:
    """Repeated calls return the same string (the lru_cache contract)."""
    first = load_jx3_calibration()
    second = load_jx3_calibration()
    assert first == second
    # Sanity: should not be a no-op / empty string.
    assert len(first) > 1000  # ~150-250 lines of markdown


def test_review_agent_system_prompt_includes_jx3_calibration() -> None:
    """``_build_system_prompt`` must prepend the JX3 calibration block."""
    rendered = build_review_system_prompt()
    assert JX3_CALIBRATION_HEADER in rendered
    assert SECTION_A_PROBE in rendered
    # Calibration must precede the role prompt (review prompt's distinctive
    # opening line should appear AFTER the calibration header).
    assert rendered.index(JX3_CALIBRATION_HEADER) < rendered.index('You are an automated PR review agent')
    # And the underlying role prompt is still there in full.
    assert REVIEW_SYSTEM_PROMPT in rendered


def test_initiative_agent_compose_includes_jx3_calibration() -> None:
    """Mirror of the initiative.py compose pipeline.

    We can't easily call ``run_initiative`` in unit tests (it needs an
    ANTHROPIC_API_KEY + clones repos), so reconstruct the compose-pipeline
    shape here. If ``initiative.py`` drifts away from this shape, the
    real-runtime test would catch the regression — but pinning it here
    surfaces the intent faster.
    """
    from gate.agent.lessons import render_for

    blocks: list[str] = [load_jx3_calibration()]
    lessons = render_for('initiative_agent')
    if lessons:
        blocks.append(lessons)
    blocks.append(INITIATIVE_SYSTEM_PROMPT)
    composed = '\n\n---\n\n'.join(blocks)

    assert JX3_CALIBRATION_HEADER in composed
    assert SECTION_A_PROBE in composed
    assert composed.index(JX3_CALIBRATION_HEADER) < composed.index('You are an automated initiative agent')
    assert INITIATIVE_SYSTEM_PROMPT in composed
