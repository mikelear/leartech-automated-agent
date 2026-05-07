"""Shared pytest fixtures + CLI options for all criteria tiers."""

from __future__ import annotations

import pytest

from gate.tools import PRContext, load_pr_context


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption('--repo', action='store', default=None, help='Repo name (mikelear/X or just X)')
    parser.addoption('--pr', action='store', default=None, type=int, help='PR number')


@pytest.fixture(scope='session')
def pr_context(request: pytest.FixtureRequest) -> PRContext:
    repo = request.config.getoption('--repo')
    pr_number = request.config.getoption('--pr')
    if not repo or not pr_number:
        pytest.skip('--repo and --pr are required to run criteria gates')
    return load_pr_context(repo, pr_number)
