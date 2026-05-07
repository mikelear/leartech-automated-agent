"""Parse + fetch + diff `.lighthouse/jenkins-x/triggers.yaml` files.

Used by two criteria:

- `test_no_silently_disabled_triggers` — consumer's own triggers vs own rollup;
  catches catalog-side regressions where the LighthouseJob is created but no
  PipelineRun materialises (e.g. the 2026-05-01 ai-review breakage).
- `test_no_drift_vs_golden_template` — consumer's triggers vs the golden template's;
  catches consumer drift when the template adds new triggers and consumers haven't
  caught up.

Golden-template mapping is hard-coded for now. When qa-architecture's `repo-type.yaml`
lands (Phase 1), we'll read the mapping from there instead.
"""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass

import yaml

# Hard-coded mapping. Refine when qa-architecture's `repo-type.yaml` lands.
# Repos not in the map skip the drift criterion (with a clear reason).
GOLDEN_TEMPLATE_FOR: dict[str, str] = {
    'leartech-auth-ui': 'mikelear/leartech-angular-service-template',
    'leartech-auth-service': 'mikelear/leartech-go-service-template',
    'leartech-soc-collector': 'mikelear/leartech-go-service-template',
    'webcoder-ui': 'mikelear/leartech-angular-service-template',
    'webcoder-service': 'mikelear/leartech-go-service-template',
    'next-generation-lending-website': 'mikelear/leartech-angular-service-template',
    'leartech-podcast-feed': 'mikelear/leartech-go-service-template',
    'leartech-transcript-service': 'mikelear/leartech-go-service-template',
}


@dataclass(frozen=True)
class Trigger:
    name: str
    context: str
    always_run: bool
    optional: bool
    source: str


def parse_triggers_yaml(text: str) -> list[Trigger]:
    """Parse a `.lighthouse/jenkins-x/triggers.yaml` body into typed Triggers.

    Skips any presubmit entry that's missing required fields rather than raising —
    we want partial parsing to still surface drift, not block on schema strictness.
    """
    data = yaml.safe_load(text) or {}
    presubmits = data.get('spec', {}).get('presubmits', []) or []
    triggers: list[Trigger] = []
    for entry in presubmits:
        if not isinstance(entry, dict):
            continue
        name = entry.get('name')
        context = entry.get('context')
        if not name or not context:
            continue
        triggers.append(
            Trigger(
                name=str(name),
                context=str(context),
                always_run=bool(entry.get('always_run', False)),
                optional=bool(entry.get('optional', False)),
                source=str(entry.get('source', '')),
            )
        )
    return triggers


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def fetch_triggers_yaml(repo: str, ref: str = 'main') -> list[Trigger]:
    """Fetch + parse a repo's `triggers.yaml` at the given ref. Empty list if absent.

    Note: `ref` goes in the URL query string. Using `-f ref=main` makes `gh` POST
    (used for creating files), which 404s.
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    try:
        raw = _gh(
            [
                'api',
                f'repos/{qualified}/contents/.lighthouse/jenkins-x/triggers.yaml?ref={ref}',
                '--jq',
                '.content',
            ]
        )
    except RuntimeError:
        return []
    decoded = base64.b64decode(raw).decode('utf-8', errors='replace')
    return parse_triggers_yaml(decoded)


def golden_template_for(repo: str) -> str | None:
    """Return the golden-template repo for `repo`, or None if not registered."""
    short = repo.split('/')[-1]
    return GOLDEN_TEMPLATE_FOR.get(short)


def angular_template_consumers() -> frozenset[str]:
    """Repos derived from `leartech-angular-service-template`.

    Used by `gate/criteria/per_repo/_angular_service_template/conftest.py` to scope
    the per-template criteria. Adding a new repo to `GOLDEN_TEMPLATE_FOR` with an
    angular-template value picks it up here automatically.
    """
    return frozenset(repo for repo, template in GOLDEN_TEMPLATE_FOR.items() if 'angular-service-template' in template)


def go_service_template_consumers() -> frozenset[str]:
    """Repos derived from `leartech-go-service-template`. Symmetric to angular_template_consumers."""
    return frozenset(repo for repo, template in GOLDEN_TEMPLATE_FOR.items() if 'go-service-template' in template)


def diff_triggers(consumer: list[Trigger], golden: list[Trigger]) -> tuple[list[str], list[str]]:
    """Returns (missing_from_consumer, extra_in_consumer) by *context* (not name).

    Only `always_run: true` triggers from the golden template are required of the consumer.
    Triggers that golden ships as `always_run: false` (e.g. `/ai-feedback`) are opt-in;
    consumer absence is not drift.
    """
    consumer_contexts = {t.context for t in consumer}
    golden_required = {t.context for t in golden if t.always_run}
    missing = sorted(golden_required - consumer_contexts)
    extra = sorted(consumer_contexts - {t.context for t in golden})
    return missing, extra
