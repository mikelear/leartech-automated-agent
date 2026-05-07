"""Unit tests for gate.tools.ui_surface_diff — pure parsers on synthetic diff fixtures."""

from __future__ import annotations

import textwrap

from gate.tools.ui_surface_diff import (
    added_files_in_diff,
    compute_ui_surface_delta,
    data_testids_in_added_lines,
    routes_in_added_lines,
    selectors_from_added_lines,
)

# Realistic diff: new Angular component file added, with selector + data-testid in template.
NEW_COMPONENT_DIFF = textwrap.dedent(
    """
    diff --git a/src/app/profile/profile.component.ts b/src/app/profile/profile.component.ts
    new file mode 100644
    index 0000000..abc1234
    --- /dev/null
    +++ b/src/app/profile/profile.component.ts
    @@ -0,0 +1,15 @@
    +import { Component, OnInit } from '@angular/core';
    +
    +@Component({
    +  standalone: false,
    +  selector: 'app-profile',
    +  templateUrl: './profile.component.html',
    +})
    +export class ProfileComponent implements OnInit {
    +  ngOnInit() {}
    +}
    diff --git a/src/app/profile/profile.component.html b/src/app/profile/profile.component.html
    new file mode 100644
    index 0000000..def5678
    --- /dev/null
    +++ b/src/app/profile/profile.component.html
    @@ -0,0 +1,5 @@
    +<div class="profile" data-testid="profile-page">
    +  <h2 data-testid="profile-heading">Profile</h2>
    +  <button data-testid="edit-button">Edit</button>
    +</div>
    diff --git a/src/app/app-routing.module.ts b/src/app/app-routing.module.ts
    index 1111111..2222222 100644
    --- a/src/app/app-routing.module.ts
    +++ b/src/app/app-routing.module.ts
    @@ -10,6 +10,7 @@ const routes: Routes = [
       { path: 'login', component: LoginComponent },
       { path: 'home', component: HomeComponent },
    +  { path: 'profile', component: ProfileComponent },
     ];
    """
).strip()


def test_added_files_picks_up_new_component_ts() -> None:
    files = added_files_in_diff(NEW_COMPONENT_DIFF, '.component.ts')
    assert files == ['src/app/profile/profile.component.ts']


def test_added_files_picks_up_new_component_html() -> None:
    files = added_files_in_diff(NEW_COMPONENT_DIFF, '.component.html')
    assert files == ['src/app/profile/profile.component.html']


def test_added_files_does_not_match_modified_files() -> None:
    """app-routing.module.ts was modified, not added — must NOT be flagged as new."""
    files = added_files_in_diff(NEW_COMPONENT_DIFF, '.module.ts')
    assert files == []


def test_selectors_extracted_from_diff() -> None:
    selectors = selectors_from_added_lines(NEW_COMPONENT_DIFF)
    assert selectors == ['app-profile']


def test_data_testids_extracted_from_diff() -> None:
    testids = data_testids_in_added_lines(NEW_COMPONENT_DIFF)
    assert sorted(testids) == ['edit-button', 'profile-heading', 'profile-page']


def test_routes_extracted_from_diff() -> None:
    routes = routes_in_added_lines(NEW_COMPONENT_DIFF)
    # Modified file but new route line — both 'profile' and the unchanged 'login'/'home' would NOT
    # be in `+` lines for unchanged content, only profile. (Verify by reading the diff.)
    assert routes == ['profile']


def test_compute_ui_surface_delta_combines_all_signals() -> None:
    delta = compute_ui_surface_delta(NEW_COMPONENT_DIFF)
    assert delta.new_component_files == ('src/app/profile/profile.component.ts',)
    assert delta.new_component_selectors == ('app-profile',)
    assert sorted(delta.new_data_testids) == ['edit-button', 'profile-heading', 'profile-page']
    assert delta.new_route_paths == ('profile',)
    assert not delta.is_empty


def test_empty_diff_yields_empty_delta() -> None:
    delta = compute_ui_surface_delta('')
    assert delta.is_empty


def test_spec_only_diff_yields_empty_delta() -> None:
    """A PR adding only a *.spec.ts file should produce no UI surface signal."""
    spec_only = textwrap.dedent(
        """
        diff --git a/src/app/login.spec.ts b/src/app/login.spec.ts
        new file mode 100644
        index 0000000..abc
        --- /dev/null
        +++ b/src/app/login.spec.ts
        @@ -0,0 +1,3 @@
        +describe('LoginComponent', () => {
        +  it('renders', () => {});
        +});
        """
    ).strip()
    delta = compute_ui_surface_delta(spec_only)
    assert delta.is_empty
