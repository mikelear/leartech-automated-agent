"""Unit tests for the video-review priority logic — pin the new-surface bias added after PR #39."""

from __future__ import annotations

from gate.criteria.per_repo._angular_service_template.test_video_review import _name_fragments, _overlap_score
from gate.tools.ui_surface_diff import UISurfaceDelta


def test_fragments_strip_app_prefix_from_selectors() -> None:
    delta = UISurfaceDelta(new_component_selectors=('app-profile',))
    fragments = _name_fragments(delta)
    assert 'profile' in fragments  # 'app-profile' → 'profile'
    assert 'app-profile' in fragments  # original kept too


def test_fragments_split_testids_for_partial_match() -> None:
    delta = UISurfaceDelta(new_data_testids=('profile-page', 'edit-button', 'x-btn'))
    fragments = _name_fragments(delta)
    # Full testids always kept
    assert 'profile-page' in fragments
    assert 'edit-button' in fragments
    assert 'x-btn' in fragments
    # Stems > 3 chars are kept; 'btn' (3 chars) is filtered out as too noisy
    assert 'profile' in fragments  # 7 chars
    assert 'page' in fragments  # 4 chars (> 3)
    assert 'edit' in fragments  # 4 chars
    assert 'button' in fragments  # 6 chars
    assert 'btn' not in fragments  # 3 chars — filter excludes it


def test_fragments_handle_routes() -> None:
    delta = UISurfaceDelta(new_route_paths=('/profile', 'admin/users'))
    fragments = _name_fragments(delta)
    assert 'profile' in fragments
    assert 'admin/users' in fragments


def test_fragments_empty_for_empty_delta() -> None:
    assert _name_fragments(UISurfaceDelta()) == set()


def test_overlap_score_counts_matches() -> None:
    fragments = {'profile', 'app-profile', 'profile-page'}
    # '03-profile-page-spec' contains 'profile' AND 'profile-page' (but NOT 'app-profile')
    assert _overlap_score('03-profile-page-spec', fragments) == 2
    # 'app-profile' substring isn't in any spec name we'd produce — count drops to 1 here
    assert _overlap_score('app-profile-renders', fragments) == 2  # 'profile' + 'app-profile'
    assert _overlap_score('01-page-loads', fragments) == 0
    assert _overlap_score('02-LOGIN-form', fragments) == 0
    # case-insensitive: 'Profile-Heading' matches 'profile'
    assert _overlap_score('Profile-Heading', fragments) == 1


def test_overlap_score_falls_to_zero_with_empty_fragments() -> None:
    assert _overlap_score('any-spec-name', set()) == 0
