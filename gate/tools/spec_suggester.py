"""AI-drafted Playwright specs for newly-introduced UI surface.

When `test_ui_changes_have_playwright_coverage` flags a gap, this tool feeds Claude:
- The new component / route / testid that lacks coverage
- 1-2 existing specs from the same project as patterns
- The constraint that the spec must follow the project's idioms

Returns a complete `*.spec.ts` body as a string. The criterion includes it in the
assertion message so a human reviewer can copy + adapt + commit.

Same anthropic-SDK + tool_use pattern as `gate/tools/video_review.py`.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, cast

from gate.tools.ui_surface_diff import UISurfaceDelta

# Gateway-portability: model id is env-configurable, never hardcoded, so a
# cluster can point this at the gateway's logical model name (or a non-Anthropic
# model) without a code change. Default keeps the prior Sonnet behaviour.
# See AI-GATEWAY-AND-PORTABILITY.md ("Don't hardcode model ids").
DEFAULT_MODEL = os.environ.get('LEARTECH_SPEC_SUGGESTER_MODEL', 'claude-sonnet-4-6')

SPEC_SUGGESTER_SYSTEM_PROMPT = """You are a Playwright spec author for the leartech engineering org.

You receive:
- A description of *new UI surface* introduced by a PR (component, route, data-testid)
- 1-2 existing Playwright specs from the same project, as structural reference
- The repo's conventions (preview URL, baseURL handling, etc. — included in the user message)

Your job: draft a complete `*.spec.ts` file that exercises the new surface, **matching
the existing specs' idioms exactly**: same imports, same describe/test structure, same
selector style (testid > role > text), same waitFor / expect patterns.

Hard rules:
- Use `data-testid` selectors when available. Never use text-content selectors.
- No `page.waitForTimeout` — use `waitFor` / `expect.toHaveX` instead.
- Don't fabricate routes, components, or testids that weren't in the input.
- Keep the spec focused: 2-4 test cases, each asserting one specific behaviour.

API discipline (CRITICAL — guards against hallucinated methods, lesson PR #40):

- **Only call Locator/Page methods that appear verbatim in the reference specs**
  (e.g. `.isVisible()`, `.click()`, `.goto()`, `.locator()`, `.getByTestId()`).
- **Never invent a method that mirrors an assertion.** `expect(loc).toBeAttached()`
  exists as an assertion; `loc.isAttached()` does NOT exist as a Locator method
  in any Playwright version. If you need a check the references don't demonstrate,
  prefer the assertion form: `await expect(loc).toBeAttached()` — never call a
  hypothetical `loc.isAttached()` method.
- The full set of `is*` methods on Locator is exactly: `isChecked`, `isDisabled`,
  `isEditable`, `isEnabled`, `isHidden`, `isVisible`. No others.

Output via the `propose_spec` tool — never reply with plain text or markdown."""

PROPOSE_SPEC_TOOL: dict[str, Any] = {
    'name': 'propose_spec',
    'description': 'Return the proposed Playwright spec file content + a short rationale.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'filename': {
                'type': 'string',
                'description': "Suggested filename relative to end2end-ui/ — e.g. '07-new-feature.spec.ts'.",
            },
            'spec_body': {
                'type': 'string',
                'description': 'Complete TypeScript spec file content. Will be written verbatim.',
            },
            'rationale': {
                'type': 'string',
                'description': 'One- or two-sentence explanation of what the spec exercises and why these test cases.',
            },
        },
        'required': ['filename', 'spec_body', 'rationale'],
    },
}


@dataclass(frozen=True)
class SpecSuggestion:
    filename: str
    spec_body: str
    rationale: str


def is_anthropic_key_present() -> bool:
    return bool(os.environ.get('ANTHROPIC_API_KEY')) and shutil.which('python') is not None


def build_user_message(
    delta: UISurfaceDelta,
    *,
    reference_specs: list[tuple[str, str]],
    component_source: str | None = None,
) -> str:
    """Construct the user prompt — describe the delta + include reference specs."""
    parts: list[str] = []
    parts.append('## New UI surface lacking Playwright coverage')
    if delta.new_component_files:
        parts.append('\n**New component files:**')
        for f in delta.new_component_files:
            parts.append(f'- `{f}`')
    if delta.new_component_selectors:
        parts.append('\n**New component selectors:**')
        for s in delta.new_component_selectors:
            parts.append(f'- `{s}`')
    if delta.new_data_testids:
        parts.append('\n**New data-testid values:**')
        for t in delta.new_data_testids:
            parts.append(f'- `{t}`')
    if delta.new_route_paths:
        parts.append('\n**New route paths:**')
        for r in delta.new_route_paths:
            parts.append(f'- `{r}`')

    if component_source:
        parts.append('\n## Source of the new component\n')
        parts.append('```typescript')
        parts.append(component_source)
        parts.append('```')

    parts.append('\n## Existing specs (use as structural reference)')
    for name, body in reference_specs:
        parts.append(f'\n### `{name}`\n')
        parts.append('```typescript')
        parts.append(body)
        parts.append('```')

    parts.append('\n## Task')
    parts.append(
        'Draft a complete spec file that exercises the new surface above. Match the '
        'existing specs idiomatically — imports, describe/test structure, selectors, waits. '
        'Use the `propose_spec` tool to return your output.'
    )
    return '\n'.join(parts)


def parse_tool_use_response(response_content: list[dict[str, Any]]) -> SpecSuggestion:
    """Walk the assistant content blocks for the propose_spec result."""
    for block in response_content:
        if block.get('type') != 'tool_use' or block.get('name') != 'propose_spec':
            continue
        tool_input = block.get('input', {})
        return SpecSuggestion(
            filename=str(tool_input.get('filename', 'new.spec.ts')),
            spec_body=str(tool_input.get('spec_body', '')),
            rationale=str(tool_input.get('rationale', '')),
        )
    raise RuntimeError('No propose_spec tool_use block in response')


def suggest_spec(
    delta: UISurfaceDelta,
    *,
    reference_specs: list[tuple[str, str]],
    component_source: str | None = None,
    model: str = DEFAULT_MODEL,
) -> SpecSuggestion:
    """Send the delta + reference specs to Claude with `propose_spec` tool. Returns the suggestion.

    Requires ANTHROPIC_API_KEY. Use `is_anthropic_key_present()` to gate the call.
    """
    from anthropic import Anthropic

    client = Anthropic()
    user_msg = build_user_message(delta, reference_specs=reference_specs, component_source=component_source)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SPEC_SUGGESTER_SYSTEM_PROMPT,
        tools=cast(Any, [PROPOSE_SPEC_TOOL]),
        tool_choice=cast(Any, {'type': 'tool', 'name': 'propose_spec'}),
        messages=cast(Any, [{'role': 'user', 'content': user_msg}]),
    )
    blocks = [block.model_dump() for block in response.content]
    return parse_tool_use_response(blocks)
