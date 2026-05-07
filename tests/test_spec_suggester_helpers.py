"""Unit tests for gate.tools.spec_suggester pure parts (prompt builder + tool-use parser).

The actual SDK call is exercised via live integration when the criterion fires;
these tests pin the surface that doesn't require an API key.
"""

from __future__ import annotations

from gate.tools.spec_suggester import (
    PROPOSE_SPEC_TOOL,
    build_user_message,
    parse_tool_use_response,
)
from gate.tools.ui_surface_diff import UISurfaceDelta


def test_user_message_includes_all_delta_signals() -> None:
    delta = UISurfaceDelta(
        new_component_files=('src/app/profile/profile.component.ts',),
        new_component_selectors=('app-profile',),
        new_data_testids=('profile-page', 'edit-button'),
        new_route_paths=('profile',),
    )
    msg = build_user_message(delta, reference_specs=[('01-page-loads.spec.ts', 'reference body')])
    assert 'app-profile' in msg
    assert 'profile-page' in msg
    assert 'edit-button' in msg
    assert "'profile'" in msg or 'profile' in msg
    assert 'reference body' in msg
    assert '01-page-loads.spec.ts' in msg


def test_user_message_omits_empty_sections() -> None:
    delta = UISurfaceDelta(
        new_component_selectors=('app-something',),
    )
    msg = build_user_message(delta, reference_specs=[])
    assert 'New component selectors' in msg
    assert 'New data-testid values' not in msg  # empty section is suppressed
    assert 'New route paths' not in msg


def test_parse_tool_use_response_extracts_all_fields() -> None:
    content = [
        {
            'type': 'tool_use',
            'name': 'propose_spec',
            'input': {
                'filename': '07-profile.spec.ts',
                'spec_body': "import { test } from 'playwright/test';\ntest('renders', () => {});",
                'rationale': 'Covers profile page render.',
            },
        }
    ]
    suggestion = parse_tool_use_response(content)
    assert suggestion.filename == '07-profile.spec.ts'
    assert suggestion.spec_body.startswith('import { test')
    assert suggestion.rationale == 'Covers profile page render.'


def test_tool_schema_has_required_fields() -> None:
    schema = PROPOSE_SPEC_TOOL['input_schema']
    assert set(schema['required']) == {'filename', 'spec_body', 'rationale'}
