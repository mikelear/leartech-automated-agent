"""Shape tests for the Grafana dashboards shipped with the chart.

Background: the dashboards live under ``charts/leartech-automated-agent/dashboards/*.json``.
The chart's ``templates/grafana-dashboards.yaml`` uses ``.Files.Glob``
to render each JSON verbatim into a labelled ConfigMap that the cluster
Grafana sidecar auto-imports (see the template file's own comment for
the pattern).

If any JSON file breaks parsing OR two dashboards collide on ``uid`` OR
the template stops globbing this directory, the Grafana sidecar silently
drops the whole set — no dashboard, no error, invisible. These tests
pin the shape so that regression is caught in the PR pipeline.

Two levers we deliberately keep loose:

- The exact panel titles / positions are NOT asserted — those change
  with every dashboard-rebuild initiative and asserting them would just
  produce churn. What matters is that each dashboard has AT LEAST ONE
  panel and every panel has an ``id`` + ``gridPos``.
- LogQL query strings are NOT asserted — the Loki datasource lives
  outside this repo and validates them at query time. We only check
  that every non-``row`` panel has at least one target with an
  ``expr``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CHART_ROOT = Path(__file__).parents[1] / 'charts' / 'leartech-automated-agent'
DASHBOARDS_DIR = CHART_ROOT / 'dashboards'
GRAFANA_TEMPLATE = CHART_ROOT / 'templates' / 'grafana-dashboards.yaml'

# The revamped 3-tier set. Adding a fourth tier is a real event — update
# this list AND the initiative's design doc. Removing one below is only
# valid if the tier's queries were merged into another dashboard.
EXPECTED_UIDS = {
    'leartech-plans',  # Tier 1 — plans.json
    'leartech-plan-and-loop',  # Tier 2 — plan-and-loop.json (headline)
    'leartech-agent-runs',  # Tier 3 — agent-runs.json (drill-down)
}


def _dashboards() -> list[Path]:
    return sorted(DASHBOARDS_DIR.glob('*.json'))


def test_dashboards_directory_is_non_empty() -> None:
    """If dashboards/ is empty, Grafana silently gets no import — the
    Files.Glob in the template returns an empty range and no ConfigMap
    is generated."""
    files = _dashboards()
    assert files, (
        f'{DASHBOARDS_DIR} must contain at least one *.json dashboard. '
        f'The chart Files.Glob would render zero ConfigMaps.'
    )


@pytest.mark.parametrize('dash', _dashboards(), ids=lambda p: p.name)
def test_dashboard_json_parses(dash: Path) -> None:
    """Grafana rejects a malformed dashboard silently on some sidecar
    versions; we fail loudly in CI instead."""
    try:
        json.loads(dash.read_text())
    except json.JSONDecodeError as exc:
        raise AssertionError(f'{dash.name} is not valid JSON: {exc}') from exc


@pytest.mark.parametrize('dash', _dashboards(), ids=lambda p: p.name)
def test_dashboard_has_uid_and_title(dash: Path) -> None:
    d = json.loads(dash.read_text())
    assert d.get('uid'), f'{dash.name} missing top-level `uid` — Grafana requires it.'
    assert d.get('title'), f'{dash.name} missing top-level `title` — Grafana requires it.'


@pytest.mark.parametrize('dash', _dashboards(), ids=lambda p: p.name)
def test_dashboard_has_panels(dash: Path) -> None:
    d = json.loads(dash.read_text())
    panels = d.get('panels', [])
    assert panels, f'{dash.name} has no panels — dashboard would render empty.'
    # Every panel needs an id and a gridPos (Grafana silently drops the
    # panel otherwise).
    for pan in panels:
        assert 'id' in pan, f'{dash.name} panel missing `id`: {pan!r}'
        assert 'gridPos' in pan, f'{dash.name} panel id={pan.get("id")} missing `gridPos`'


@pytest.mark.parametrize('dash', _dashboards(), ids=lambda p: p.name)
def test_dashboard_panel_ids_are_unique(dash: Path) -> None:
    d = json.loads(dash.read_text())
    ids = [pan['id'] for pan in d.get('panels', [])]
    assert len(ids) == len(set(ids)), (
        f'{dash.name} has duplicate panel ids: {ids}. Grafana permalink anchors (?viewPanel=<id>) collide.'
    )


@pytest.mark.parametrize('dash', _dashboards(), ids=lambda p: p.name)
def test_non_row_panels_have_targets(dash: Path) -> None:
    """A row is a header/divider — no query. Everything else must have
    a query target (`targets: [{expr: ...}]`) or Grafana renders it
    with 'No data'."""
    d = json.loads(dash.read_text())
    for pan in d.get('panels', []):
        if pan.get('type') == 'row':
            continue
        targets = pan.get('targets', [])
        assert targets, (
            f'{dash.name} panel id={pan.get("id")} type={pan.get("type")} '
            f'has no targets. Grafana would render "No data".'
        )
        for i, t in enumerate(targets):
            assert 'expr' in t, f'{dash.name} panel id={pan.get("id")} target #{i} missing `expr`.'


def test_uids_are_unique_across_all_dashboards() -> None:
    """Grafana dedups by uid — two dashboards with the same uid means
    one silently wins and the other is invisible."""
    seen: dict[str, str] = {}
    for dash in _dashboards():
        uid = json.loads(dash.read_text())['uid']
        assert uid not in seen, (
            f'duplicate dashboard uid `{uid}` in {dash.name} and {seen[uid]} — Grafana would drop one silently.'
        )
        seen[uid] = dash.name


def test_expected_tier_uids_are_all_present() -> None:
    """The 3-tier structure — Plans / Event→BA Loop / Agent Runs — is
    the design shape. Renames land here. Removals require follow-up."""
    actual = {json.loads(dash.read_text())['uid'] for dash in _dashboards()}
    missing = EXPECTED_UIDS - actual
    assert not missing, f'expected dashboard uids missing: {sorted(missing)}. Got: {sorted(actual)}.'


def test_chart_template_globs_dashboards_directory() -> None:
    """The template must Glob dashboards/*.json — otherwise a newly
    added dashboard file would sit in the tree with no ConfigMap being
    generated for it."""
    text = GRAFANA_TEMPLATE.read_text()
    assert '.Files.Glob "dashboards/*.json"' in text, (
        'templates/grafana-dashboards.yaml must Files.Glob the dashboards/*.json path so all dashboards ship together.'
    )
    assert '.Files.Get' in text, (
        'templates/grafana-dashboards.yaml must .Files.Get each JSON '
        'file verbatim (helm-templating would break Grafana `{{label}}` '
        'legend syntax).'
    )
    assert 'jenkins-x.io/grafana-dashboard' in text, (
        'templates/grafana-dashboards.yaml must label the ConfigMap with '
        '`jenkins-x.io/grafana-dashboard: "1"` so the cluster Grafana '
        'sidecar (searchNamespace: ALL) picks it up.'
    )


# ---------------------------------------------------------------------
# Structured-logging vocabulary check
# ---------------------------------------------------------------------
# Post-rebuild, panels are supposed to query via `| json | <field>=…`
# (per Hub/status/structured-logging-standard.md), not by substring greps.
# The initiative allows a line-filter FALLBACK where a stream may not
# be JSON yet — so we don't ban substring filters entirely, but we DO
# require at least one `| json` pipeline stage across the new-tier
# dashboards. If a new dashboard is added, this test nudges the author
# toward the standard vocabulary.


@pytest.mark.parametrize(
    'dash_uid',
    ['leartech-plans', 'leartech-plan-and-loop', 'leartech-agent-runs'],
)
def test_dashboard_queries_use_json_parser(dash_uid: str) -> None:
    """At least one panel target on each tier must call `| json` —
    otherwise the dashboard is still substring-grepping (the anti-pattern
    the initiative was chartered to fix)."""
    dash = next(p for p in _dashboards() if json.loads(p.read_text()).get('uid') == dash_uid)
    d = json.loads(dash.read_text())
    has_json = False
    for pan in d.get('panels', []):
        for t in pan.get('targets', []):
            if '| json' in t.get('expr', ''):
                has_json = True
                break
        if has_json:
            break
    assert has_json, (
        f'{dash.name} (uid={dash_uid}) has zero panels using `| json` — '
        f'the rebuild standard is structured queries. See '
        f'Hub/status/structured-logging-standard.md.'
    )
