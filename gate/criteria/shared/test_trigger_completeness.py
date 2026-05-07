"""Trigger-completeness criteria — guards against catalog regressions and consumer drift.

Two failure modes this catches:

1. **Catalog-side regression**: a trigger declared `always_run: true` in the consumer's
   `triggers.yaml` produces no GitHub status check at all. Symptom of the 2026-05-01
   ai-review breakage — LighthouseJob created but never materialised into a PipelineRun.
2. **Consumer drift**: the golden-template (`leartech-{angular,go}-service-template`)
   ships `always_run: true` triggers that the consumer hasn't picked up — silent
   coverage gap relative to the org standard.
"""

from __future__ import annotations

import pytest

from gate.tools import (
    PRContext,
    diff_triggers,
    fetch_triggers_yaml,
    golden_template_for,
    list_pr_checks,
)

pytestmark = pytest.mark.shared


def test_no_silently_disabled_triggers(pr_context: PRContext) -> None:
    """Every `always_run: true` trigger in the consumer's triggers.yaml must produce a check.

    If a trigger is configured but absent from the rollup, the catalog source is broken
    (this is what the 2026-05-01 ai-review classifier-pre-check change did across the org —
    LighthouseJob created, controller reconciled in a loop, no PipelineRun ever materialised,
    GitHub never saw a status check).
    """
    triggers = fetch_triggers_yaml(pr_context.repo)
    declared_required = {t.context for t in triggers if t.always_run}
    if not declared_required:
        pytest.skip(f'No triggers.yaml found for {pr_context.repo} (or no always_run:true entries).')

    rollup_contexts = {check.check for check in list_pr_checks(pr_context.repo, pr_context.number)}
    silently_missing = sorted(declared_required - rollup_contexts)
    assert not silently_missing, (
        'Triggers configured `always_run: true` but producing no GitHub status check '
        '(catalog-side regression?):\n  ' + '\n  '.join(silently_missing)
    )


def test_no_drift_vs_golden_template(pr_context: PRContext) -> None:
    """Consumer's triggers.yaml must include every `always_run: true` trigger from its golden template.

    Compares context names (not source paths or step bodies — that's a deeper drift check
    we'd surface separately). Skips if no golden template is registered for this repo.
    """
    template = golden_template_for(pr_context.repo)
    if template is None:
        pytest.skip(f'No golden template registered for {pr_context.repo} — drift check N/A.')

    consumer_triggers = fetch_triggers_yaml(pr_context.repo)
    golden_triggers = fetch_triggers_yaml(template)
    if not golden_triggers:
        pytest.skip(f'Golden template {template} has no readable triggers.yaml — drift check N/A.')

    missing, _extra = diff_triggers(consumer_triggers, golden_triggers)
    assert not missing, (
        f'Consumer is missing required triggers from golden template {template}:\n  ' + '\n  '.join(missing) + '\n'
        '(Consumer-specific extras are not surfaced as failures — only missing-from-golden.)'
    )
