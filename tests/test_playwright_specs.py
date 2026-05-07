"""Unit tests for gate.tools.playwright_specs — spec inventory parsing."""

from __future__ import annotations

import textwrap

from gate.tools.playwright_specs import SpecCoverage, parse_spec

REAL_SPEC = textwrap.dedent(
    """
    import { test, expect } from 'playwright/test';

    test.describe('login flow', () => {
      test('shows login page on /login', async ({ page }) => {
        await page.goto('/login');
        const form = page.getByTestId('login-form');
        await expect(form).toBeVisible();
        await page.locator('app-login').waitFor();
      });

      test('submit click', async ({ page }) => {
        await page.goto('/');
        const btn = page.locator('[data-testid="submit-button"]');
        await btn.click();
      });
    });
    """
).strip()


def test_extracts_routes_from_page_goto() -> None:
    routes, _, _ = parse_spec(REAL_SPEC)
    assert routes == {'/login', '/'}


def test_extracts_testids_via_get_by_testid() -> None:
    _, testids, _ = parse_spec(REAL_SPEC)
    assert 'login-form' in testids


def test_extracts_testids_via_locator_attribute() -> None:
    _, testids, _ = parse_spec(REAL_SPEC)
    assert 'submit-button' in testids


def test_extracts_component_selectors() -> None:
    _, _, selectors = parse_spec(REAL_SPEC)
    assert selectors == {'app-login'}


def test_covers_route_supports_prefix_match() -> None:
    cov = SpecCoverage(routes=frozenset({'two-factor'}))
    assert cov.covers_route('two-factor')
    assert cov.covers_route('/two-factor')
    assert cov.covers_route('two-factor/setup')  # nested route covered by parent
    assert not cov.covers_route('home')


def test_covers_testid_exact_match() -> None:
    cov = SpecCoverage(data_testids=frozenset({'login-form'}))
    assert cov.covers_testid('login-form')
    assert not cov.covers_testid('login')
