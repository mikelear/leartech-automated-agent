"""CLI entrypoint — `gate check --repo X --pr N` runs criteria gates against a live PR."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

CRITERIA_ROOT = Path(__file__).parent / 'criteria'


@click.group()
def main() -> None:
    """Criteria-driven gate runner for leartech."""


@main.command()
@click.option('--repo', required=True, help='Repo name (mikelear/X or just X).')
@click.option('--pr', required=True, type=int, help='PR number.')
@click.option(
    '--mark',
    default=None,
    help='Pytest marker to filter to (e.g. shared, unit, integration, e2e, playwright).',
)
@click.option('-v', '--verbose', is_flag=True, default=True, help='Verbose pytest output (default on).')
def check(repo: str, pr: int, mark: str | None, verbose: bool) -> None:
    """Run criteria gates against a live PR. Exits 0 if all green, non-zero on any failure."""
    args = ['pytest', str(CRITERIA_ROOT), '--repo', repo, '--pr', str(pr)]
    if mark:
        args.extend(['-m', mark])
    if verbose:
        args.append('-v')
    args.extend(['--json-report', '--json-report-file=.report.json'])

    result = subprocess.run(args, check=False)
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()
