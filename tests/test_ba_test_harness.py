"""Tests for gate.agent.ba_test_harness — the BA test harness that proves
BA-authored plans satisfy the draft-by-default + verification invariants
WITHOUT firing repo-factory-scale work.

The harness is a pure-function validator: given a Brief and a list of
authored plan dicts (as YAML/JSON), assert the invariants hold. The
three GOLDEN fixtures under ``tests/testdata/ba_briefs/`` exercise the
three scenarios the initiative names:

* ``infra-remediation`` — a single failing plan is resolved by a single
  authored plan (the most common shape).
* ``new-website`` — greenfield, ``resolves: []``, no ``remediates`` on
  the authored plan.
* ``cluster-wide-multi-resolve`` — two PlanRefs in ``resolves`` produce
  two authored plans, one per cluster, and the collection-level check
  enforces coverage.

Negative-path tests mutate a golden fixture to violate each invariant
and confirm :class:`PlanShapeError` is raised with a clear message. This
gives us both a positive proof (the goldens are internally consistent)
and a negative proof (each invariant is actually load-bearing).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from gate.agent import ba_agent
from gate.agent.ba_test_harness import (
    CANONICAL_VERIFICATION_ACTION,
    VERIFICATION_ACTION_MARKERS,
    PlanShapeError,
    validate_authored_plan,
    validate_authored_plans,
)

FIXTURE_ROOT = Path(__file__).parent / 'testdata' / 'ba_briefs'

GOLDEN_FIXTURES = ('infra-remediation', 'new-website', 'cluster-wide-multi-resolve')


# --- Fixture loading ---------------------------------------------------------


def _load_brief(fixture_name: str) -> ba_agent.Brief:
    path = FIXTURE_ROOT / fixture_name / 'brief.yaml'
    return ba_agent.load_brief(path.read_text(encoding='utf-8'))


def _load_expected_plans(fixture_name: str) -> list[dict[str, Any]]:
    path = FIXTURE_ROOT / fixture_name / 'expected-plans.yaml'
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert isinstance(data, list), f'{path} must be a top-level list of plans'
    return data


# --- Golden happy-path tests -------------------------------------------------


@pytest.mark.parametrize('fixture_name', GOLDEN_FIXTURES)
def test_golden_fixture_validates(fixture_name: str) -> None:
    """Each golden fixture's expected-plans must satisfy the harness."""
    brief = _load_brief(fixture_name)
    plans = _load_expected_plans(fixture_name)
    # Per-plan validation is called from collection validation, but hitting
    # both surfaces here guards against a future refactor breaking either.
    for plan in plans:
        validate_authored_plan(brief, plan)
    validate_authored_plans(brief, plans)


def test_infra_remediation_produces_one_plan_with_one_remediate() -> None:
    """Sanity check on shape — one PlanRef -> one plan -> one remediate."""
    brief = _load_brief('infra-remediation')
    plans = _load_expected_plans('infra-remediation')
    assert len(brief.resolves) == 1
    assert len(plans) == 1
    remediates = plans[0]['spec']['remediates']
    assert len(remediates) == 1
    assert remediates[0]['name'] == brief.resolves[0].name
    assert remediates[0]['namespace'] == brief.resolves[0].namespace


def test_new_website_plan_has_no_remediates() -> None:
    """Greenfield brief -> no smuggled `remediates`."""
    brief = _load_brief('new-website')
    plans = _load_expected_plans('new-website')
    assert brief.resolves == []
    assert len(plans) == 1
    # `remediates` may be absent, None, or []; the harness rejects a
    # non-empty list, so confirm it's one of those three.
    remediates = plans[0]['spec'].get('remediates') or []
    assert remediates == []


def test_cluster_wide_multi_resolve_covers_all_planrefs() -> None:
    """Two PlanRefs -> two plans -> UNION of `remediates` covers both."""
    brief = _load_brief('cluster-wide-multi-resolve')
    plans = _load_expected_plans('cluster-wide-multi-resolve')
    assert len(brief.resolves) == 2
    assert len(plans) == 2
    covered: set[tuple[str, str]] = set()
    for plan in plans:
        for item in plan['spec'].get('remediates') or []:
            covered.add((item['name'], item['namespace']))
    expected = {(r.name, r.namespace) for r in brief.resolves}
    assert covered == expected


@pytest.mark.parametrize('fixture_name', GOLDEN_FIXTURES)
def test_every_golden_final_step_is_verification_shaped(fixture_name: str) -> None:
    """Explicit assertion: last step's action matches a verification marker.

    Piggybacks on the harness's own check, but makes the intent explicit —
    if a future golden slipped a non-verification action into the last
    step, this test would flag it independently.
    """
    plans = _load_expected_plans(fixture_name)
    for plan in plans:
        steps = plan['spec']['steps']
        assert steps, f'plan {plan["metadata"]["name"]} has no steps'
        final_action = steps[-1]['inputs']['action']
        assert any(marker in final_action for marker in VERIFICATION_ACTION_MARKERS), (
            f'plan {plan["metadata"]["name"]} final action {final_action!r} '
            f'does not match any verification marker in {VERIFICATION_ACTION_MARKERS!r}'
        )


@pytest.mark.parametrize('fixture_name', GOLDEN_FIXTURES)
def test_every_golden_carries_draft_annotation_and_hold_true(fixture_name: str) -> None:
    """Draft-by-default invariant — spelt out per fixture."""
    plans = _load_expected_plans(fixture_name)
    for plan in plans:
        annotations = plan.get('metadata', {}).get('annotations', {})
        assert annotations.get(ba_agent.DRAFT_ANNOTATION_KEY) == ba_agent.DRAFT_ANNOTATION_VALUE, (
            f'plan {plan["metadata"]["name"]} missing draft annotation'
        )
        assert plan['spec']['hold'] is True, f'plan {plan["metadata"]["name"]} not held'


# --- Negative-path tests (each invariant is load-bearing) --------------------


def _clone_plan(fixture_name: str, index: int = 0) -> dict[str, Any]:
    """Deep-copy one plan out of a golden fixture (so mutations don't leak)."""
    plans = _load_expected_plans(fixture_name)
    return copy.deepcopy(plans[index])


def test_missing_draft_annotation_raises() -> None:
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    plan['metadata']['annotations'] = {}
    with pytest.raises(PlanShapeError, match='not draft-annotated'):
        validate_authored_plan(brief, plan)


def test_wrong_draft_annotation_value_raises() -> None:
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    plan['metadata']['annotations'][ba_agent.DRAFT_ANNOTATION_KEY] = 'false'
    with pytest.raises(PlanShapeError, match='not draft-annotated'):
        validate_authored_plan(brief, plan)


def test_hold_false_raises() -> None:
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    plan['spec']['hold'] = False
    with pytest.raises(PlanShapeError, match='not held'):
        validate_authored_plan(brief, plan)


def test_missing_hold_raises() -> None:
    """`hold` absent from spec is as bad as `hold: false`."""
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    del plan['spec']['hold']
    with pytest.raises(PlanShapeError, match='not held'):
        validate_authored_plan(brief, plan)


def test_empty_steps_raises() -> None:
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    plan['spec']['steps'] = []
    with pytest.raises(PlanShapeError, match='steps is empty'):
        validate_authored_plan(brief, plan)


def test_final_step_not_verification_raises() -> None:
    """A plan whose last step does something OTHER than verify fails
    the "final step must verify" invariant."""
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    plan['spec']['steps'][-1]['inputs']['action'] = 'deploy-config'
    with pytest.raises(PlanShapeError, match='not a verification step'):
        validate_authored_plan(brief, plan)


def test_verification_step_present_but_not_last_raises() -> None:
    """Even if a verification step exists, it MUST be last — otherwise
    a later step could invalidate the passed verification."""
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    # Swap the order — verify becomes penultimate.
    plan['spec']['steps'].append(
        {
            'name': 'post-verify-work',
            'agentType': 'leartech-agent-infra',
            'repo': '',
            'inputs': {'action': 'deploy-config'},
        }
    )
    with pytest.raises(PlanShapeError, match='not a verification step'):
        validate_authored_plan(brief, plan)


def test_smuggled_remediates_on_empty_resolves_raises() -> None:
    """New-website brief resolves nothing; a plan claiming remediation is
    smuggled state that the harness catches."""
    brief = _load_brief('new-website')
    plan = _clone_plan('new-website')
    plan['spec']['remediates'] = [{'name': 'sneaky', 'namespace': 'jx-staging'}]
    with pytest.raises(PlanShapeError, match='brief.resolves is empty'):
        validate_authored_plan(brief, plan)


def test_empty_remediates_on_nonempty_resolves_raises() -> None:
    """A brief with `resolves` must produce plans with matching remediates."""
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    plan['spec']['remediates'] = []
    with pytest.raises(PlanShapeError, match='remediates is empty'):
        validate_authored_plan(brief, plan)


def test_smuggled_planref_addition_raises() -> None:
    """A remediates entry not in the brief's resolves is a scribe bug."""
    brief = _load_brief('infra-remediation')
    plan = _clone_plan('infra-remediation')
    plan['spec']['remediates'].append({'name': 'unrelated', 'namespace': 'jx-elsewhere'})
    with pytest.raises(PlanShapeError, match='not in brief.resolves|does not correspond'):
        validate_authored_plan(brief, plan)


def test_multi_resolve_missing_one_cluster_raises() -> None:
    """If only ONE of the brief's cluster PlanRefs is remediated, coverage
    is incomplete and the collection-level check must fail."""
    brief = _load_brief('cluster-wide-multi-resolve')
    plans = _load_expected_plans('cluster-wide-multi-resolve')
    # Drop the second plan (az-side); coverage should now fail.
    reduced = plans[:1]
    with pytest.raises(PlanShapeError, match='not covered'):
        validate_authored_plans(brief, reduced)


def test_empty_plan_list_raises() -> None:
    brief = _load_brief('infra-remediation')
    with pytest.raises(PlanShapeError, match='zero plans'):
        validate_authored_plans(brief, [])


def test_collection_error_message_names_offending_plan() -> None:
    """A per-plan error surfaced from the collection validator should
    include the offending plan's metadata name to speed up debugging."""
    brief = _load_brief('cluster-wide-multi-resolve')
    plans = _load_expected_plans('cluster-wide-multi-resolve')
    # Break the SECOND plan; the message must name it.
    plans[1]['spec']['hold'] = False
    with pytest.raises(PlanShapeError, match=r'draft-unstick-foo-service-az.*not held'):
        validate_authored_plans(brief, plans)


# --- Broad shape robustness (malformed inputs don't crash) -------------------


@pytest.mark.parametrize(
    'malformed_plan',
    [
        {},  # nothing at all
        {'metadata': None, 'spec': None},  # None-typed sub-fields
        {'metadata': 'not-a-dict', 'spec': 'nope'},  # scalar
        {'spec': {'steps': 'not-a-list'}},  # steps is scalar
    ],
)
def test_malformed_plan_dict_raises_planshapeerror_not_typeerror(
    malformed_plan: dict[str, Any],
) -> None:
    """Feeding the harness junk should still raise PlanShapeError with a
    diagnostic, not a bare TypeError / KeyError."""
    brief = _load_brief('infra-remediation')
    with pytest.raises(PlanShapeError):
        validate_authored_plan(brief, malformed_plan)


# --- Verification-action markers ---------------------------------------------


def test_canonical_verification_action_is_in_markers() -> None:
    assert any(marker in CANONICAL_VERIFICATION_ACTION for marker in VERIFICATION_ACTION_MARKERS), (
        'CANONICAL_VERIFICATION_ACTION must satisfy the harness itself; '
        'otherwise the canonical fixture would be its own counter-example'
    )


def test_alt_verification_action_names_are_accepted() -> None:
    """The harness accepts any action containing a verification marker
    (health-check / verify / health_check) — future BA output should not
    need a harness rev to add a new verification-shaped action."""
    brief = _load_brief('infra-remediation')
    for alt in ('deploy-health-check', 'verify-webhook', 'kubectl_health_check'):
        plan = _clone_plan('infra-remediation')
        plan['spec']['steps'][-1]['inputs']['action'] = alt
        # Should NOT raise:
        validate_authored_plan(brief, plan)


def test_individual_stage_actions_are_accepted_as_verification() -> None:
    """The five individual single-stage release-check actions
    (release-status / promote-status / verify-gate / boot-status /
    deploy-health) are all verification-shaped — a decomposed
    release-shepherd Plan's final step is typically ``deploy-health``,
    and the harness must accept it (else BA-authored decomposed chains
    would be rejected as non-verification-terminated)."""
    brief = _load_brief('infra-remediation')
    for action in (
        'release-status',
        'promote-status',
        'verify-gate',
        'boot-status',
        'deploy-health',
    ):
        plan = _clone_plan('infra-remediation')
        plan['spec']['steps'][-1]['inputs']['action'] = action
        # Should NOT raise — the plan ends on a valid individual stage-action.
        validate_authored_plan(brief, plan)


# --- Fixture file layout is stable -------------------------------------------


@pytest.mark.parametrize('fixture_name', GOLDEN_FIXTURES)
def test_fixture_dir_has_the_three_expected_files(fixture_name: str) -> None:
    """Each golden fixture ships THREE files: brief, expected-plans, and
    a documentation snapshot of platform_state. The snapshot is not
    consumed by the harness but documents the scenario for future
    operators; missing it makes the golden less reproducible."""
    d = FIXTURE_ROOT / fixture_name
    assert (d / 'brief.yaml').is_file(), f'{d}/brief.yaml missing'
    assert (d / 'expected-plans.yaml').is_file(), f'{d}/expected-plans.yaml missing'
    assert (d / 'platform-state-snapshot.yaml').is_file(), f'{d}/platform-state-snapshot.yaml missing'
