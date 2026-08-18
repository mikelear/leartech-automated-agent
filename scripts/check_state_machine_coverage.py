"""D3 — per-module state-machine coverage gate.

Reads ``coverage.xml`` (Cobertura format produced by ``coverage xml``)
and asserts each V5-D1/D2 state-machine module meets the per-file
floors:

- **Statement coverage >= 85%** (``line-rate`` in the XML)
- **Branch coverage   >= 70%** (``branch-rate`` in the XML)

Exits non-zero with a clear, file-by-file diff when ANY floor is
breached. Designed to run after ``pytest --cov --cov-report=xml`` in
the PR pipeline (see ``.lighthouse/jenkins-x/pullrequest.yaml`` —
``state-machine-coverage-gate`` step).

## Why a separate script (not just ``coverage report --fail-under``)?

The global ``[tool.coverage.report] fail_under`` is a single floor across
the whole codebase. The state-machine modules carry a stricter,
per-file contract pinned by the D2 invariants: regressing coverage on
``job_reconciler`` or ``app.state`` is a regression of the run-driver's
correctness story, even if the global average still looks healthy.
This script is the per-file enforcement.

## Module list

State-machine modules are listed in ``STATE_MACHINE_MODULES`` below.
Each is a path relative to the repo root. To add a new module:

1. Add the path to ``STATE_MACHINE_MODULES``.
2. Make sure it is NOT in ``[tool.coverage.run] omit`` (see
   ``pyproject.toml``), else it won't appear in ``coverage.xml`` and
   this gate will treat it as a missing-source error.
3. Confirm the new module currently meets the floors, or add the tests
   that bring it up to the floor BEFORE landing the gate change.

## XML schema notes

Cobertura schema (what ``coverage xml`` writes):

```
<coverage>
  <sources>
    <source>/path/to/source-root-A</source>
    <source>/path/to/source-root-B</source>
  </sources>
  <packages>
    <package name="...">
      <classes>
        <class filename="relative/path.py" line-rate="0.95" branch-rate="0.80">
          ...
```

``filename`` is relative to one of the ``<source>`` roots. We resolve
each candidate by joining against every ``<source>`` and accepting the
first absolute path that exists. This makes the gate robust against
``coverage.py`` collapsing a single repo into multiple package roots
(here, ``gate/`` and ``app/`` both appear as ``<source>`` entries).
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET  # noqa: S405  # input is a trusted build artifact (see _load_coverage)
from pathlib import Path

STATE_MACHINE_MODULES: tuple[str, ...] = ('gate/agent/agentrun_client.py',)

STATEMENT_THRESHOLD: float = 0.85
BRANCH_THRESHOLD: float = 0.70


def _load_coverage(xml_path: Path) -> dict[str, tuple[float, float]]:
    """Parse ``coverage.xml`` → ``{absolute_path: (line_rate, branch_rate)}``.

    Joins each ``<class filename=...>`` against every ``<source>`` root
    and accepts the first existing absolute path. Files that don't
    resolve (e.g. stale entries from an earlier run) are silently
    dropped — they can't be the state-machine modules anyway because
    those paths are repo-rooted and stable.
    """
    tree = ET.parse(xml_path)  # noqa: S314
    root = tree.getroot()
    sources = [Path(s.text) for s in root.findall('sources/source') if s.text]

    measured: dict[str, tuple[float, float]] = {}
    for cls in root.iter('class'):
        filename = cls.get('filename', '')
        if not filename:
            continue
        line_rate = float(cls.get('line-rate', '0') or 0)
        branch_rate = float(cls.get('branch-rate', '0') or 0)
        for src in sources:
            candidate = src / filename
            if candidate.exists():
                measured[str(candidate.resolve())] = (line_rate, branch_rate)
                break
    return measured


def _check_module(
    repo_root: Path,
    module_rel: str,
    measured: dict[str, tuple[float, float]],
) -> tuple[bool, list[str], str]:
    """Validate one module. Returns ``(ok, failures, summary_line)``."""
    abs_path = str((repo_root / module_rel).resolve())
    if abs_path not in measured:
        return (
            False,
            [
                f'{module_rel}: not present in coverage.xml. '
                'Check that `[tool.coverage.run] omit` in pyproject.toml '
                'does NOT exclude this path, and that the test run '
                'instrumented the file.',
            ],
            f'  ? {module_rel}: NOT MEASURED',
        )
    line_rate, branch_rate = measured[abs_path]
    failures: list[str] = []
    if line_rate < STATEMENT_THRESHOLD:
        delta = STATEMENT_THRESHOLD - line_rate
        failures.append(
            f'{module_rel}: statement coverage {line_rate:.1%} < '
            f'threshold {STATEMENT_THRESHOLD:.0%} '
            f'(short by {delta:.1%})'
        )
    if branch_rate < BRANCH_THRESHOLD:
        delta = BRANCH_THRESHOLD - branch_rate
        failures.append(
            f'{module_rel}: branch coverage {branch_rate:.1%} < threshold {BRANCH_THRESHOLD:.0%} (short by {delta:.1%})'
        )
    mark = '+' if not failures else '-'
    summary = (
        f'  {mark} {module_rel}: stmt {line_rate:.1%} '
        f'(>= {STATEMENT_THRESHOLD:.0%}), '
        f'branch {branch_rate:.1%} (>= {BRANCH_THRESHOLD:.0%})'
    )
    return (not failures, failures, summary)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_root = Path(__file__).resolve().parents[1]
    xml_path = Path(argv[0]).resolve() if argv else (repo_root / 'coverage.xml').resolve()

    if not xml_path.exists():
        print(
            f'ERROR: coverage.xml not found at {xml_path}.\n'
            'Run `uv run pytest --cov --cov-report=xml tests/` first '
            '(or pass an explicit path as argv[1]).',
            file=sys.stderr,
        )
        return 2

    measured = _load_coverage(xml_path)

    print(f'=== D3 state-machine coverage gate ({xml_path.name}) ===')
    print(f'Thresholds: statement >= {STATEMENT_THRESHOLD:.0%}, branch >= {BRANCH_THRESHOLD:.0%} per file')
    print()

    all_failures: list[str] = []
    summaries: list[str] = []
    for module_rel in STATE_MACHINE_MODULES:
        _ok, failures, summary = _check_module(repo_root, module_rel, measured)
        summaries.append(summary)
        all_failures.extend(failures)

    for line in summaries:
        print(line)
    print()

    if all_failures:
        print('FAIL — state-machine coverage gate breached:')
        for failure in all_failures:
            print(f'  * {failure}')
        print()
        print(
            'Fix: add (or restore) tests covering the uncovered lines / '
            'branches in the offending module(s). The D1/D2 state-machine '
            'tests live in tests/agent/test_run_driver_state_machine.py, '
            'tests/test_job_reconciler.py, tests/test_job_runner.py, '
            'tests/test_state_orphan_respect_job.py.'
        )
        return 1

    print('PASS — every state-machine module meets per-file floors.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
