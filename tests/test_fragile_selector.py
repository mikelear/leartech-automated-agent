"""Unit tests for is_fragile_text_selector — pinning the heuristic so false positives don't sneak back in."""

from __future__ import annotations

from gate.tools.playwright_artifacts import is_fragile_text_selector

# Patterns we DO want to flag — actual text-based selectors that break on copy edits.
FRAGILE_LINES = [
    "await page.getByText('Sign in').click();",
    'expect(page.getByText(/welcome/i)).toBeVisible();',
    "await page.locator('text=Submit').click();",
    'await page.locator("text=Login").click();',
    'await page.locator(\':has-text("Welcome")\').click();',
    'await page.locator(\':text("Sign out")\').click();',
]

# Patterns we do NOT want to flag — structural CSS / attribute / testid selectors.
SAFE_LINES = [
    'const el = page.locator(\'[data-testid="authenticated-page"]\');',
    "await page.locator('input[type=\"password\"]').fill('hunter2');",
    "await page.locator('.error-banner').count();",
    "const x = await page.getByTestId('login-form');",
    "await page.locator('app-home').waitFor();",
    '// getByText is the old way — use getByTestId',  # comment, no actual call
    "await page.click('#submit');",
]


def test_flags_real_text_selectors() -> None:
    for line in FRAGILE_LINES:
        assert is_fragile_text_selector(line), f'should flag: {line!r}'


def test_does_not_flag_structural_selectors() -> None:
    for line in SAFE_LINES:
        assert not is_fragile_text_selector(line), f'should NOT flag: {line!r}'
