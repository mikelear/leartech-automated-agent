"""Sanity-check the JX3 calibration markdown file is shippable.

Tests:
1. The file exists at the expected path inside the package.
2. It isn't empty and is reasonably sized (calibration target ~150-250 lines).
3. It contains all four required sections (A/B/C/D/E per the initiative spec).
4. It mentions the cross-referenced memory entries — those cross-refs are how
   the calibration stays grounded to existing real-world failures.

If a future edit drops a section or breaks the file path, these pin it.
"""

from __future__ import annotations

from pathlib import Path

import gate.agent.calibrations as calibrations_pkg

CALIBRATION_PATH = Path(calibrations_pkg.__file__).parent / 'jx3-full-flow.md'


def test_calibration_file_exists_at_package_path() -> None:
    assert CALIBRATION_PATH.exists(), f'Missing calibration markdown at {CALIBRATION_PATH}'


def test_calibration_file_is_non_empty_and_reasonable_size() -> None:
    text = CALIBRATION_PATH.read_text(encoding='utf-8')
    # Strip whitespace before counting so a file of only newlines doesn't pass.
    assert text.strip(), 'Calibration markdown is empty.'
    # Target is ~150-250 lines per the initiative spec; allow a wider band.
    line_count = len(text.splitlines())
    assert 80 <= line_count <= 400, f'Unexpected line count: {line_count} (target ~150-250).'


def test_calibration_contains_required_sections() -> None:
    text = CALIBRATION_PATH.read_text(encoding='utf-8')
    # Section headings — match on stable substrings, allow tone reflows.
    required_markers = [
        '## A. The rough shape',
        '## B. How to find the truth',
        '## C. Local-test checklist',
        '## D. Chatops command reference',
        '## E. Cluster + registry + version variability',
    ]
    missing = [m for m in required_markers if m not in text]
    assert not missing, f'Calibration missing required sections: {missing}'


def test_calibration_cross_references_memory_entries() -> None:
    """The calibration MUST cite the memory files it mirrors.

    Cross-refs keep the calibration grounded in real failures the agents
    have hit. If a future edit strips them, the calibration drifts toward
    abstract advice — exactly what the lesson-as-calibration discipline
    is supposed to prevent.
    """
    text = CALIBRATION_PATH.read_text(encoding='utf-8')
    expected_refs = [
        'feedback_ai_review_does_not_apply_approved_label',
        'feedback_consult_reference_cluster_before_iterating_on_single_cluster_failure',
        'feedback_async_tests_need_event_not_sleep',
        'feedback_lighthouse_retest_syntax',
    ]
    missing = [ref for ref in expected_refs if ref not in text]
    assert not missing, f'Calibration missing memory cross-refs: {missing}'


def test_calibration_warns_against_test_with_cluster_prefix() -> None:
    """Pin the chatops syntax gotcha — `/test gcp/pr` does NOT fire anything."""
    text = CALIBRATION_PATH.read_text(encoding='utf-8')
    # The substantive warning should mention that prefixed names don't work.
    assert '/test gcp/pr' in text or '/test <cluster>/<check>' in text
    assert 'strips the cluster prefix' in text or 'silently dropped' in text


def test_calibration_parses_as_markdown_round_trip() -> None:
    """Basic sanity: file is valid UTF-8 and has matched fences if any are used."""
    text = CALIBRATION_PATH.read_text(encoding='utf-8')
    # Triple-backtick fences must be balanced (even count of ``` occurrences).
    fence_count = text.count('```')
    assert fence_count % 2 == 0, f'Unbalanced ``` fences: {fence_count} occurrences.'
    # No NUL bytes / control chars beyond \t, \n, \r.
    bad = [c for c in text if ord(c) < 32 and c not in '\t\n\r']
    assert not bad, f'Found {len(bad)} control characters in calibration markdown.'
