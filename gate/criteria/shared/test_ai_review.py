"""AI code review criteria — sticky-comment driven (ai-review posts no GitHub check)."""

from __future__ import annotations

import pytest

from gate.tools import AIReviewVerdict, PRContext, read_ai_review_verdicts

pytestmark = pytest.mark.shared


@pytest.fixture(scope='session')
def ai_review_verdicts(pr_context: PRContext) -> list[AIReviewVerdict]:
    verdicts = read_ai_review_verdicts(pr_context.repo, pr_context.number)
    if not verdicts:
        pytest.skip(f'No AI review comments posted yet for #{pr_context.number}')
    return verdicts


def test_ai_review_not_blocking(ai_review_verdicts: list[AIReviewVerdict]) -> None:
    """No AI review verdict is a hard blocker (`:x:`)."""
    blocking = [v for v in ai_review_verdicts if v.blocking]
    assert not blocking, 'AI review hard-blockers:\n' + '\n'.join(
        f'  [{v.cluster}] {v.score}/100 — {v.verdict}' for v in blocking
    )


def test_ai_review_passing_on_every_cluster(ai_review_verdicts: list[AIReviewVerdict]) -> None:
    """Every cluster's AI review reports `:white_check_mark:` (full pass), not `:warning:` (needs work).

    Stricter than `test_ai_review_not_blocking` — `:warning:` doesn't block auto-merge but
    indicates concrete reviewer findings worth addressing before client review.
    """
    not_passing = [v for v in ai_review_verdicts if not v.passed]
    assert not not_passing, 'AI review verdicts below pass threshold:\n' + '\n'.join(
        f'  [{v.cluster}] :{v.emoji}: {v.score}/100 — {v.verdict}' for v in not_passing
    )
