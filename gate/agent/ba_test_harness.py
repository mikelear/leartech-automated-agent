"""BA test harness — pure-function validator for BA-authored Plan CRDs.

The BA agent (``gate/agent/ba_agent.py``) consumes a BRIEF and authors one or
more DRAFT Plan CRDs via the ``create_plan`` / ``amend_plan`` MCPs. Every
authored plan must satisfy a small set of INVARIANTS derived from the brief:

1.  **Draft-by-default.** Plan spec has ``hold: true`` AND
    ``metadata.annotations["leartech.io/draft"] == "true"``. This is the
    contract that stops downstream (Job spawn, PR opens, …) from firing
    off a BA-authored plan without a human clearing the hold.

2.  **Final step verifies successCriteria.** The last step in
    ``spec.steps`` must be a verification step — canonically
    ``inputs.action: deploy-health`` (the deterministic, version-aware
    release-verify check), but any action whose name contains "health-check"
    or "verify" is accepted so future verification shapes don't force a
    harness rebuild. Without this the plan can "succeed" without satisfying
    the brief.

3.  **`remediates` matches `resolves`.** If the brief carries
    ``resolves: [PlanRef, …]``, the plan's ``spec.remediates`` list must
    include EVERY PlanRef (by ``{name, namespace}``). If the brief's
    ``resolves`` is empty (e.g. new-website brief), each plan's
    ``remediates`` must be empty/absent — a remediates on a plan with
    nothing to remediate is a scribe bug.

4.  **`spec.steps` non-empty.** A plan with no steps trivially "succeeds"
    without doing any work — same failure mode as (2).

The harness exists to prove BA end-to-end **without firing repo-factory-scale
work**. Concretely:

- **Unit-test path** — hand-crafted brief YAML + hand-crafted expected-plans
  YAML → :func:`validate_authored_plan` per plan → the golden tests in
  ``tests/test_ba_test_harness.py`` demonstrate that both the invariants and
  the fixtures are internally consistent, so a real BA that matches the
  fixture shape is provably conformant.

- **On-cluster fast path** — see ``docs/BA-TEST-HARNESS.md``. Author a
  standalone BA AgentRun with a brief; the BA authors a DRAFT (held) Plan
  which never auto-executes. Operator inspects with ``kubectl get plans``,
  ``kubectl delete plan`` if unhappy, iterates.

This module is intentionally provider-agnostic — it does not import
``anthropic``, ``claude_agent_sdk``, or ``httpx``. It operates on plain
Python dicts (as parsed from YAML). See ``AI-GATEWAY-AND-PORTABILITY.md``.
"""

from __future__ import annotations

from typing import Any

from gate.agent.ba_agent import DRAFT_ANNOTATION_KEY, DRAFT_ANNOTATION_VALUE, Brief

# Canonical verification action name. Verification is now the single
# DETERMINISTIC ``deploy-health`` check (version-aware; see
# ``gate/agent/release_checks.py``) — it superseded the legacy LLM-transcribed
# ``release-health-check`` STAGE_STATUS action. New verification shapes should
# be added to :data:`VERIFICATION_ACTION_MARKERS` rather than replacing this
# constant so existing fixtures continue to match.
CANONICAL_VERIFICATION_ACTION = 'deploy-health'

# Substrings the harness accepts as "verification-shaped" in a step's
# ``inputs.action``. Loose match on purpose — a future BA that authors
# ``verify-webhook-response`` or ``check-deployment-health`` for a
# non-infra-agent verification path should not need a harness rev.
#
# The canonical verification is the deterministic ``deploy-health`` check
# (version-aware, authoritative in-cluster verdict). ``promote-status`` (the
# other deterministic release-verify check) is also accepted, as are the
# generic ``health-check`` / ``verify`` shapes for forward compatibility.
VERIFICATION_ACTION_MARKERS: tuple[str, ...] = (
    'health-check',
    'verify',
    'health_check',
    # Deterministic release-verify check action names (kind: check).
    'promote-status',
    'deploy-health',
)


class PlanShapeError(AssertionError):
    """Raised by :func:`validate_authored_plan` when an invariant is violated.

    Subclasses ``AssertionError`` so pytest reports the message inline and
    the raise-site behaves like a plain assertion — no special handler
    required in tests.
    """


def _get_annotations(plan: dict[str, Any]) -> dict[str, Any]:
    """Return ``metadata.annotations`` (or ``{}``) — never raises on shape."""
    meta = plan.get('metadata') or {}
    if not isinstance(meta, dict):
        return {}
    annotations = meta.get('annotations') or {}
    return annotations if isinstance(annotations, dict) else {}


def _get_spec(plan: dict[str, Any]) -> dict[str, Any]:
    """Return ``spec`` (or ``{}``) — never raises on shape."""
    spec = plan.get('spec') or {}
    return spec if isinstance(spec, dict) else {}


def _get_steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``spec.steps`` (or ``[]``) — never raises on shape."""
    steps = _get_spec(plan).get('steps') or []
    return list(steps) if isinstance(steps, list) else []


def _get_remediates(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``spec.remediates`` (or ``[]``) — never raises on shape.

    We coerce to a normalised list-of-dicts so callers can use set
    comparisons on ``{name, namespace}`` pairs without special-casing
    absent/None/scalar shapes.
    """
    remediates = _get_spec(plan).get('remediates') or []
    if not isinstance(remediates, list):
        return []
    return [item for item in remediates if isinstance(item, dict)]


def _final_step_is_verification(steps: list[dict[str, Any]]) -> bool:
    """A step qualifies as verification if its ``inputs.action`` matches one
    of :data:`VERIFICATION_ACTION_MARKERS`.

    We inspect only ``inputs.action`` — not the step ``name`` — because the
    name is free-form ("verify", "check-live", "release-check", …) while the
    action is the machine-readable contract with the executing agent.
    """
    if not steps:
        return False
    final = steps[-1]
    if not isinstance(final, dict):
        return False
    inputs = final.get('inputs') or {}
    if not isinstance(inputs, dict):
        return False
    action = inputs.get('action')
    if not isinstance(action, str):
        return False
    return any(marker in action for marker in VERIFICATION_ACTION_MARKERS)


def _planref_key(item: dict[str, Any]) -> tuple[str, str]:
    """Canonical ``(name, namespace)`` key for PlanRef set comparisons."""
    return (str(item.get('name') or ''), str(item.get('namespace') or ''))


def validate_authored_plan(brief: Brief, plan: dict[str, Any]) -> None:
    """Validate one BA-authored plan against the brief's contract.

    Raises :class:`PlanShapeError` with a specific message on the first
    invariant violation. On success, returns ``None``.

    ``plan`` is a plain dict, typically deserialised from YAML — e.g. the
    payload of ``create_plan(plan=<...>)`` on the control-plane MCP.

    Invariants checked, in order:

    1.  ``metadata.annotations["leartech.io/draft"] == "true"``.
    2.  ``spec.hold`` is truthy.
    3.  ``spec.steps`` is a non-empty list.
    4.  The final step's ``inputs.action`` is verification-shaped.
    5.  ``spec.remediates`` matches ``brief.resolves`` as a set of
        ``(name, namespace)`` pairs — including the "both empty" case.
    """
    # (1) draft annotation
    annotations = _get_annotations(plan)
    draft = annotations.get(DRAFT_ANNOTATION_KEY)
    if str(draft).lower() != DRAFT_ANNOTATION_VALUE.lower():
        raise PlanShapeError(
            f'plan is not draft-annotated: expected metadata.annotations[{DRAFT_ANNOTATION_KEY!r}] '
            f'== {DRAFT_ANNOTATION_VALUE!r}, got {draft!r}'
        )

    # (2) hold: true
    spec = _get_spec(plan)
    if not spec.get('hold'):
        raise PlanShapeError(
            f'plan is not held (spec.hold is falsy: {spec.get("hold")!r}); '
            'BA-authored plans MUST be draft-by-default so downstream does not fire without human review'
        )

    # (3) non-empty steps
    steps = _get_steps(plan)
    if not steps:
        raise PlanShapeError('plan.spec.steps is empty — a plan with no steps trivially "succeeds" without doing work')

    # (4) final step is verification-shaped
    if not _final_step_is_verification(steps):
        final = steps[-1] if steps else None
        final_action = (final.get('inputs') or {}).get('action') if isinstance(final, dict) else None
        raise PlanShapeError(
            f'plan.spec.steps final step is not a verification step '
            f'(inputs.action={final_action!r}); the BA must append a step matching one of '
            f'{VERIFICATION_ACTION_MARKERS!r} that verifies the brief successCriteria'
        )

    # (5) remediates ↔ resolves
    brief_resolves = {(ref.name, ref.namespace) for ref in brief.resolves}
    plan_remediates = {_planref_key(item) for item in _get_remediates(plan)}

    if (
        brief_resolves
        and not plan_remediates.issuperset(brief_resolves)
        and not brief_resolves.issuperset(plan_remediates)
    ):
        # Neither is a superset of the other — the plan is remediating a
        # different set of PlanRefs than the brief authorised. That's a
        # scribe bug regardless of direction.
        raise PlanShapeError(
            f'plan.spec.remediates {sorted(plan_remediates)!r} does not correspond to '
            f'brief.resolves {sorted(brief_resolves)!r}'
        )

    if brief_resolves:
        # Every plan the BA authors under a multi-resolve brief must
        # target AT LEAST one of the brief's resolves. Empty is not OK.
        if not plan_remediates:
            raise PlanShapeError(
                f'plan.spec.remediates is empty but brief.resolves is {sorted(brief_resolves)!r} — '
                'the BA must copy resolves into each authored plan.remediates so downstream can trace'
            )
        # And every resolves entry appearing on the plan must be one the
        # brief authorised (no smuggled additions).
        smuggled = plan_remediates - brief_resolves
        if smuggled:
            raise PlanShapeError(
                f'plan.spec.remediates includes PlanRefs {sorted(smuggled)!r} that are not in '
                f'brief.resolves {sorted(brief_resolves)!r} — the BA must not smuggle new remediation targets'
            )
    else:
        # Brief has no resolves (e.g. new-website brief) — plan must not
        # claim to remediate anything.
        if plan_remediates:
            raise PlanShapeError(
                f'plan.spec.remediates is {sorted(plan_remediates)!r} but brief.resolves is empty — '
                'a plan cannot remediate what the brief did not authorise'
            )


def validate_authored_plans(brief: Brief, plans: list[dict[str, Any]]) -> None:
    """Validate the whole collection of plans a BA authored for one brief.

    Composed of per-plan :func:`validate_authored_plan` calls plus two
    collection-level checks:

    - **At least one plan.** A BA session that authors nothing when the
      brief has ``resolves`` non-empty is either a bug (missed the target)
      or a decision the BA must have explicitly declined via its final
      report; either way, callers of the harness want a loud failure.
      Callers that expect "no plan authored" (e.g. because live state was
      already healthy) should call the per-plan validator directly on
      the empty list — the collection-level "≥1 plan" rule is opt-out.

    - **Cluster-wide multi-resolve coverage.** When the brief resolves
      multiple PlanRefs (typically one per cluster), the UNION of every
      plan's ``spec.remediates`` must cover ``brief.resolves`` — no
      resolves entry may be left un-remediated by any plan.
    """
    if not plans:
        raise PlanShapeError(
            'BA authored zero plans; if this is intentional (e.g. platform state '
            'was already healthy), skip the collection-level validator and reason '
            'about the BA final-report message directly'
        )

    for i, plan in enumerate(plans):
        try:
            validate_authored_plan(brief, plan)
        except PlanShapeError as exc:
            plan_name = _plan_display_name(plan, fallback=f'plan[{i}]')
            raise PlanShapeError(f'{plan_name}: {exc}') from None

    # Collection-level multi-resolve coverage.
    if brief.resolves:
        covered: set[tuple[str, str]] = set()
        for plan in plans:
            for item in _get_remediates(plan):
                covered.add(_planref_key(item))
        expected = {(ref.name, ref.namespace) for ref in brief.resolves}
        missing = expected - covered
        if missing:
            raise PlanShapeError(
                f'brief.resolves {sorted(missing)!r} not covered by any authored '
                f'plan.spec.remediates (covered={sorted(covered)!r})'
            )


def _plan_display_name(plan: dict[str, Any], *, fallback: str) -> str:
    """Best-effort ``metadata.name`` for error messages; falls back on index."""
    meta = plan.get('metadata') or {}
    if isinstance(meta, dict):
        name = meta.get('name')
        if isinstance(name, str) and name:
            return name
    return fallback


__all__ = [
    'CANONICAL_VERIFICATION_ACTION',
    'PlanShapeError',
    'VERIFICATION_ACTION_MARKERS',
    'validate_authored_plan',
    'validate_authored_plans',
]
