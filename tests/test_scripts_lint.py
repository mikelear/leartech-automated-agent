"""Bash syntax + convention checks for scripts/.

The scripts under ``scripts/`` are operator helpers — they do not run
inside any CI pipeline today, so a typo can sit unnoticed until an
operator reaches for the script and discovers a missing ``fi``. These
tests pin the cheap things:

1. Every ``*.sh`` file under ``scripts/`` parses with ``bash -n``.
2. If ``shellcheck`` is available on the test image, every script
   passes the default shellcheck ruleset (warnings allowed, errors not).
3. No script invokes ``gh`` in a poll loop — the convention is to
   prefer ``kubectl`` for cluster-side state (see ``scripts/SCRIPTS.md``).
   This guards against regressions where a quick helper accidentally
   reintroduces the GraphQL-quota burn pattern that
   ``feedback_clone_path_uses_graphql_burns_quota`` warned about.

These are bash-level checks only — no kubectl / no cluster interaction.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parents[1] / 'scripts'


def _bash_scripts() -> list[Path]:
    """All ``*.sh`` files under ``scripts/``."""
    return sorted(SCRIPTS_DIR.glob('*.sh'))


def test_scripts_dir_exists() -> None:
    """Sanity: the scripts/ directory is on disk and non-empty."""
    assert SCRIPTS_DIR.is_dir(), f'expected scripts/ at {SCRIPTS_DIR}'
    assert _bash_scripts(), 'no *.sh files found under scripts/ — has the layout changed?'


@pytest.mark.parametrize('script', _bash_scripts(), ids=lambda p: p.name)
def test_bash_syntax_clean(script: Path) -> None:
    """``bash -n <script>`` must exit 0 (parses cleanly)."""
    result = subprocess.run(
        ['bash', '-n', str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f'{script.name} failed bash syntax check:\nstdout: {result.stdout}\nstderr: {result.stderr}'
    )


@pytest.mark.parametrize('script', _bash_scripts(), ids=lambda p: p.name)
def test_shellcheck_passes(script: Path) -> None:
    """If shellcheck is available, every script passes the default ruleset.

    Skipped when shellcheck is not installed (e.g. on the lint pipeline
    image which only carries python tooling). The pipeline still gets
    coverage from ``test_bash_syntax_clean``.
    """
    shellcheck = shutil.which('shellcheck')
    if shellcheck is None:
        pytest.skip('shellcheck not available on this image')

    result = subprocess.run(
        [shellcheck, '--severity=error', str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f'{script.name} failed shellcheck (errors only):\nstdout: {result.stdout}\nstderr: {result.stderr}'
    )


# Match a `gh` invocation as a command word — start of line or after `|`,
# `&&`, `;`, `$(`, backtick, or whitespace. We deliberately avoid matching
# `gh` substring inside other tokens (e.g. `length`, `weight`, etc.).
_GH_COMMAND_RE = re.compile(r'(^|[\s|&;`$(])gh\s')


@pytest.mark.parametrize('script', _bash_scripts(), ids=lambda p: p.name)
def test_no_gh_command_in_scripts(script: Path) -> None:
    """Scripts must prefer kubectl over gh for cluster-side state.

    See ``scripts/SCRIPTS.md`` for the convention and the memory
    ``feedback_clone_path_uses_graphql_burns_quota`` for why this
    matters. If a new helper genuinely needs ``gh`` (e.g. for PR
    body / merge state), the right move is to add it to the
    SCRIPTS.md "when gh is the right tool" list and document why
    it's not in a poll loop — then update this test to allow that
    script by name.
    """
    text = script.read_text()
    # Strip comment lines so the convention can still be discussed in headers.
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith('#')]
    code = '\n'.join(code_lines)

    matches = _GH_COMMAND_RE.findall(code)
    assert not matches, (
        f'{script.name} invokes `gh` — prefer kubectl for cluster-side state. '
        f'See scripts/SCRIPTS.md for the convention.'
    )
