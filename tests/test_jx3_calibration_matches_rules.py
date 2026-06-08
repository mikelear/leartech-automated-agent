"""Drift-detection: keep the JX3 calibration markdown in sync with rules.py.

The calibration markdown at `gate/agent/calibrations/jx3-full-flow.md` is the
human-readable reflection of `gate/agent/jx3/rules.py`. If someone adds a new
stage or chatops command in code without updating the doc (or vice versa),
this test fails — surfacing the drift before it reaches production.

The calibration markdown lands in a SISTER initiative
(`add-jx3-full-flow-calibration`). Until that PR merges, this test SKIPS
rather than failing — it only runs once the markdown is on disk. Once both
PRs have landed, the test enforces drift detection on every CI run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.agent.jx3.rules import JX3Stage

CALIBRATION = Path('gate/agent/calibrations/jx3-full-flow.md')


def _read_or_skip() -> str:
    """Return the calibration markdown contents, or skip if the file
    doesn't exist yet (sister initiative not merged)."""
    if not CALIBRATION.exists():
        pytest.skip(
            f'{CALIBRATION} not present yet — sister initiative '
            '(`add-jx3-full-flow-calibration`) has not landed. This test '
            'starts enforcing drift once the markdown is on disk.'
        )
    return CALIBRATION.read_text()


def test_every_stage_appears_in_calibration() -> None:
    """Every JX3Stage enum value must be referenced verbatim in the
    calibration markdown. Adding a stage in code without mentioning it
    in the doc is the most common form of drift; fail loudly on it."""
    content = _read_or_skip()
    missing = [stage.value for stage in JX3Stage if stage.value not in content]
    assert not missing, (
        f'Stages missing from calibration markdown: {missing}. '
        f'Update {CALIBRATION} when you change JX3Stage, or vice versa.'
    )


def test_required_chatops_commands_appear_in_calibration() -> None:
    """The chatops commands the rules emit (`/retest`, `/test`, `/hold cancel`)
    must be documented in the calibration markdown."""
    content = _read_or_skip()
    required = ['/retest', '/test ', '/hold cancel']
    missing = [cmd for cmd in required if cmd not in content]
    assert not missing, (
        f'Chatops commands missing from calibration: {missing}. '
        'These are emitted by gate.agent.jx3.rules.required_actions — '
        'documenting them in the markdown lets human reviewers verify '
        'the agent / Orch behaviour.'
    )
