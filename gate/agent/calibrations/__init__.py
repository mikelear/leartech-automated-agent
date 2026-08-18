"""Static calibration documents prepended to agent system prompts.

These are markdown files shipped INSIDE the wheel (not env-var injected) so
every deployment gets the same calibration without operator-side config. The
``LEARTECH_AGENT_CALIBRATIONS`` env var on top of this still composes — the

The cache is process-lifetime — file changes require a redeploy anyway since
the markdown is baked into the image at build time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_CALIBRATIONS_DIR = Path(__file__).parent

JX3_CALIBRATION_HEADER = '# JX3 Platform Calibration'
JX3_CALIBRATION_FOOTER = '# End calibration'


@lru_cache(maxsize=1)
def load_jx3_calibration() -> str:
    """Return the JX3-full-flow calibration markdown wrapped in delimiters.

    Wrapping the file contents in a clearly-named header + footer makes the
    block easy to locate in the rendered system prompt — useful for both
    test assertions and human inspection of agent prompts.
    """
    body = (_CALIBRATIONS_DIR / 'jx3-full-flow.md').read_text(encoding='utf-8').strip()
    return f'{JX3_CALIBRATION_HEADER}\n{body}\n{JX3_CALIBRATION_FOOTER}'
