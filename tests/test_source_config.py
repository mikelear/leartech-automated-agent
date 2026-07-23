"""Tests for gate.tools.source_config — deterministic repo registration (local CoS).

Cluster CoS (deferred): register-source-config PR merges and Lighthouse picks up the new
repo. Here we prove the text edit: correct entry, APPENDED to the list, idempotent, and a
MINIMAL diff that preserves comments (the file carries meaningful ones — e.g. the GCP
dockerfiles denial note). The git/gh PR wrapper is cluster-proven.
"""

from __future__ import annotations

import pytest

from gate.tools import source_config

FIXTURE = """\
apiVersion: gitops.jenkins-x.io/v1alpha1
kind: SourceConfig
spec:
  groups:
  - owner: mikelear
    provider: https://github.com
    providerKind: github
    providerName: github
    scheduler: in-repo
    repositories:
    - name: leartech-website
      description: "Leartech Website Shell"
    # leartech-dockerfiles omitted on GCP — ghcr write_package denial
    - name: leartech-auth-service
      description: "Auth service — Go"
"""


def test_entry_rendering_with_and_without_description() -> None:
    assert source_config.source_config_entry('hello-go') == '    - name: hello-go\n'
    assert source_config.source_config_entry('hello-go', 'A hello service') == (
        '    - name: hello-go\n      description: "A hello service"\n'
    )
    # embedded double-quote is escaped
    assert '\\"' in source_config.source_config_entry('x', 'a "quoted" bit')


def test_append_is_minimal_diff_and_preserves_comment() -> None:
    new_text, changed = source_config.add_repo_to_source_config(FIXTURE, 'hello-go', 'Hello world')
    assert changed
    # the ONLY change is two appended lines — removing them yields the original byte-for-byte
    added = '    - name: hello-go\n      description: "Hello world"\n'
    assert new_text.endswith(added)
    assert new_text[: -len(added)] == FIXTURE
    # the mid-list comment survives
    assert '# leartech-dockerfiles omitted on GCP' in new_text


def test_appended_after_last_repo_inside_the_list() -> None:
    new_text, _ = source_config.add_repo_to_source_config(FIXTURE, 'hello-go')
    lines = [ln.strip() for ln in new_text.splitlines()]
    assert lines.index('- name: hello-go') > lines.index('- name: leartech-auth-service')


def test_idempotent_when_already_registered() -> None:
    text2, changed = source_config.add_repo_to_source_config(FIXTURE, 'leartech-auth-service', 'dup')
    assert changed is False
    assert text2 == FIXTURE


def test_is_registered() -> None:
    assert source_config.is_registered(FIXTURE, 'leartech-website')
    assert not source_config.is_registered(FIXTURE, 'nope')


def test_missing_repositories_key_raises() -> None:
    with pytest.raises(ValueError, match='no `repositories:` key'):
        source_config.add_repo_to_source_config('spec:\n  groups: []\n', 'x')


def test_cluster_keys_match_overlay_repos() -> None:
    # register targets the same GitOps repos the overlay tool uses (gcp/az)
    assert set(source_config.CLUSTER_OVERLAY_REPOS) == {'gcp', 'az'}
