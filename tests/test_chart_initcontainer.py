"""Chart-shape tests for the migrations initContainer.

Background: Phase D's migrations originally ran as a Helm
`post-install,post-upgrade` Job hook. Hooks don't fire reliably under
JX3's helmfile-based apply path — D.4's runtime+job_name migration
(0003) didn't apply on the first cascade and the API CrashLooped on a
missing column until manual `kubectl exec psql` recovered it. We
replaced the hook with a Deployment initContainer that re-applies the
idempotent SQL on every pod start.

These tests pin the shape of the new pattern so the next migration
doesn't quietly regress. The chart isn't rendered via `helm template`
here (no helm CLI in the gate image); the templates are validated as
text-with-Go-templating using simple regex/substring assertions. That's
sufficient because the things we care about (init container present,
correct env wiring, correct mount, no leftover hook annotations) are
all stable string surfaces.
"""

from __future__ import annotations

import re
from pathlib import Path

CHART_ROOT = Path(__file__).parents[1] / 'charts' / 'leartech-automated-agent'
DEPLOYMENT = CHART_ROOT / 'templates' / 'deployment.yaml'
MIGRATIONS_CONFIGMAP = CHART_ROOT / 'templates' / 'migrations-configmap.yaml'
TEMPLATES_DIR = CHART_ROOT / 'templates'


# ---------------------------------------------------------------------------
# Old Helm-hook artefacts must be gone
# ---------------------------------------------------------------------------


def test_no_helm_hook_annotations_remain_in_chart() -> None:
    """`helm.sh/hook` was the unreliable apply path — no template should
    carry the annotation anymore. If a future migration template adds
    one this test fires before we ship the regression."""
    offenders: list[str] = []
    for yaml_path in TEMPLATES_DIR.glob('*.yaml'):
        text = yaml_path.read_text()
        if 'helm.sh/hook' in text:
            offenders.append(yaml_path.name)
    assert not offenders, (
        f'chart templates still carry helm.sh/hook annotations: {offenders}. '
        f'Migrations moved to a Deployment initContainer — see '
        f'templates/deployment.yaml and templates/migrations-configmap.yaml.'
    )


def test_migrations_job_template_removed() -> None:
    """The old `migrations-job.yaml` carried both the ConfigMap and the
    Job as Helm hooks. The Job is gone; the ConfigMap moved to
    `migrations-configmap.yaml`. Keep the obsolete filename out so
    helmfile doesn't pick up two ConfigMaps."""
    assert not (TEMPLATES_DIR / 'migrations-job.yaml').exists(), (
        'templates/migrations-job.yaml must be removed — the ConfigMap '
        'now lives in templates/migrations-configmap.yaml and the Job '
        'has been replaced by an initContainer on the Deployment.'
    )


# ---------------------------------------------------------------------------
# ConfigMap template: same payload, no hooks
# ---------------------------------------------------------------------------


def test_migrations_configmap_template_exists() -> None:
    assert MIGRATIONS_CONFIGMAP.exists(), (
        'templates/migrations-configmap.yaml must exist — the '
        'initContainer mounts it as the source of /migrations/*.sql.'
    )


def test_migrations_configmap_gated_on_same_toggle_as_initcontainer() -> None:
    """The ConfigMap and the Deployment's initContainer must light up
    together — both gated on the same `postgresql.enabled` +
    `postgresql.runMigrations` pair. Diverging gates would either
    mount an empty volume or render an orphan ConfigMap."""
    text = MIGRATIONS_CONFIGMAP.read_text()
    assert 'and .Values.postgresql.enabled .Values.postgresql.runMigrations' in text


def test_migrations_configmap_glob_includes_all_sql_files() -> None:
    """Authoritative source is files/migrations/*.sql in lexical order.
    The ConfigMap must Glob that path and Get each file inline."""
    text = MIGRATIONS_CONFIGMAP.read_text()
    assert '.Files.Glob "files/migrations/*.sql"' in text
    assert '.Files.Get' in text


# ---------------------------------------------------------------------------
# Deployment initContainer: presence, env wiring, mount
# ---------------------------------------------------------------------------


def test_deployment_renders_migrations_initcontainer() -> None:
    """initContainers block must be present, named `migrations`, and
    gated on the same toggle as the ConfigMap."""
    text = DEPLOYMENT.read_text()
    # Find the initContainers block and assert the name shows up inside
    # it (not just anywhere in the file — a future containers[].name
    # collision should not satisfy this test).
    match = re.search(
        r'\{\{-?\s*if and \.Values\.postgresql\.enabled '
        r'\.Values\.postgresql\.runMigrations\s*\}\}\s*'
        r'(?:#.*\n\s*)*'
        r'initContainers:\s*\n\s*- name: migrations',
        text,
    )
    assert match, (
        'deployment.yaml must declare an initContainers block named '
        '`migrations` gated on postgresql.enabled+runMigrations.'
    )


def test_deployment_initcontainer_wires_dsn_from_same_secret_as_main_container() -> None:
    """DSN single source of truth: both the API container and the
    migrations initContainer must read from
    `postgresql.dsn.secretName` + `postgresql.dsn.secretKey`. If the
    initContainer drifts, schema applies could land in a different
    database than the API talks to."""
    text = DEPLOYMENT.read_text()
    # Both env entries must source `postgresql.dsn.secretName` /
    # `secretKey` — count both refs.
    assert text.count('.Values.postgresql.dsn.secretName') >= 2
    assert text.count('.Values.postgresql.dsn.secretKey') >= 2


def test_deployment_initcontainer_mounts_migrations_volume() -> None:
    """initContainer mounts `/migrations` from a ConfigMap volume named
    `migrations` that points at `<Release.Name>-migrations`."""
    text = DEPLOYMENT.read_text()
    assert re.search(
        r'volumeMounts:\s*\n\s*- name: migrations\s*\n\s*mountPath: /migrations',
        text,
    ), 'initContainer must mount the migrations volume at /migrations'
    assert re.search(
        r'- name: migrations\s*\n\s*configMap:\s*\n\s*name: \{\{ \.Release\.Name \}\}-migrations',
        text,
    ), 'deployment volumes must include a `migrations` ConfigMap volume backed by `<Release.Name>-migrations`.'


def test_deployment_initcontainer_uses_psql_to_apply_migrations() -> None:
    """The applier command must shell out to `psql` with
    `ON_ERROR_STOP=1` so a SQL error fails the initContainer (and the
    pod) loudly — silent partial apply was the regression we're
    protecting against."""
    text = DEPLOYMENT.read_text()
    assert 'psql "$DATABASE_URL"' in text
    assert 'ON_ERROR_STOP=1' in text
    # Iterate over /migrations/*.sql in lexical (shell-glob) order.
    assert 'for f in /migrations/*.sql' in text


def test_deployment_initcontainer_block_only_renders_when_postgres_enabled() -> None:
    """No initContainers on preview environments that disable postgres
    — otherwise the pod would CrashLoop trying to mount a non-existent
    ConfigMap. Both the initContainer block AND the volume block must
    be guarded."""
    text = DEPLOYMENT.read_text()
    init_block_count = text.count('{{- if and .Values.postgresql.enabled .Values.postgresql.runMigrations }}')
    # One for the initContainers block, one for the volumes block.
    assert init_block_count == 2, (
        f'expected exactly 2 postgresql-gated blocks in deployment.yaml '
        f'(initContainer + volume), found {init_block_count}.'
    )
