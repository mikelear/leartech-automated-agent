"""Deterministic repo registration in a cluster's JX3 source-config.

Registering a new service on a cluster = adding one entry to
``.jx/gitops/source-config.yaml`` (``spec.groups[].repositories[]``) in that cluster's
GitOps repo and opening a PR. This is a mechanical, byte-predictable edit — so the infra
agent does it via this TOOL, not by hand-editing YAML (same principle as the repo-factory
rename tool; see memory project_repo_factory_init).

Grounded against the live GitOps repos (2026-07-23): the schema is a single owner group
(``owner: mikelear``, group-level ``scheduler: in-repo``) whose ``repositories:`` is a list
of ``{name, description?}`` — no per-repo fields. New repos are APPENDED to the end (not
alphabetised). The file carries meaningful comments (e.g. the ``leartech-dockerfiles`` GCP
denial note) and per-cluster description text, so we edit at the TEXT level to keep the diff
minimal and preserve comments — NOT via yaml.dump (which would reformat the whole file and
drop comments). Registration is per-cluster: one PR each on gcp + az (see
``CLUSTER_OVERLAY_REPOS``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gate.tools.chart_overlay import CLUSTER_OVERLAY_REPOS

# The source-config lives at the same path in both cluster GitOps repos.
SOURCE_CONFIG_PATH = '.jx/gitops/source-config.yaml'


def source_config_entry(name: str, description: str | None = None, *, indent: int = 4) -> str:
    """Render the exact YAML text for one ``repositories[]`` entry (trailing newline).

    ``indent`` is the column of the list dash (matches ``repositories:`` — 4 spaces in the
    live files); the ``description`` continuation sits at ``indent + 2``.
    """
    pad = ' ' * indent
    out = f'{pad}- name: {name}\n'
    if description:
        esc = description.replace('\\', '\\\\').replace('"', '\\"')
        out += f'{pad}  description: "{esc}"\n'
    return out


def is_registered(text: str, name: str) -> bool:
    """True if ``name`` already appears as a ``- name: <name>`` entry in the source-config."""
    target = f'- name: {name}'
    return any(line.strip() == target for line in text.splitlines())


def add_repo_to_source_config(text: str, name: str, description: str | None = None) -> tuple[str, bool]:
    """Append a repo entry to ``spec.groups[].repositories[]``. Returns ``(new_text, changed)``.

    IDEMPOTENT: if ``name`` is already registered, returns the text unchanged with
    ``changed=False`` (safe to re-run — e.g. a Plan retry). Otherwise a TEXT-level insert at
    the end of the repositories list, preserving all comments/formatting — a minimal diff.
    """
    if is_registered(text, name):
        return text, False

    lines = text.splitlines(keepends=True)

    repo_idx: int | None = None
    repo_indent = 0
    for i, ln in enumerate(lines):
        body = ln.rstrip('\n')
        stripped = body.lstrip(' ')
        if stripped.startswith('repositories:'):
            repo_idx = i
            repo_indent = len(body) - len(stripped)
            break
    if repo_idx is None:
        raise ValueError('source-config has no `repositories:` key')

    # Walk the list block: it continues while lines are blank or indented at least to the
    # list column; a dedent (a new group `- owner:` or a sibling key) ends it. block_end is
    # the last line index that belongs to the list.
    block_end = repo_idx
    j = repo_idx + 1
    while j < len(lines):
        body = lines[j].rstrip('\n')
        if body.strip() == '':
            j += 1
            continue
        indent = len(body) - len(body.lstrip(' '))
        if indent < repo_indent:
            break
        block_end = j
        j += 1

    if not lines[block_end].endswith('\n'):
        lines[block_end] = lines[block_end] + '\n'
    lines.insert(block_end + 1, source_config_entry(name, description, indent=repo_indent))
    return ''.join(lines), True


def _run(args: list[str], cwd: str | os.PathLike[str] | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'{" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def register_source_config(
    *,
    service: str,
    cluster: str,
    workdir: str | os.PathLike[str],
    description: str | None = None,
    branch: str | None = None,
) -> str:
    """Register ``service`` in ``cluster``'s source-config and open a PR. Returns the PR URL.

    Returns ``''`` (no PR) if the service is ALREADY registered — idempotent. ``cluster`` is
    a ``CLUSTER_OVERLAY_REPOS`` key (``gcp``/``az``); one call per cluster. ``workdir`` must
    not exist.
    """
    gitops = CLUSTER_OVERLAY_REPOS.get(cluster)
    if gitops is None:
        raise ValueError(f'unknown cluster {cluster!r}; expected one of {sorted(CLUSTER_OVERLAY_REPOS)}')

    work = Path(workdir)
    _run(['git', 'clone', f'https://github.com/{gitops}.git', str(work)])
    cfg = work / SOURCE_CONFIG_PATH
    new_text, changed = add_repo_to_source_config(cfg.read_text(), service, description)
    if not changed:
        return ''

    cfg.write_text(new_text)
    branch = branch or f'register-{service}'
    _run(['git', 'checkout', '-b', branch], cwd=work)
    _run(['git', 'add', SOURCE_CONFIG_PATH], cwd=work)
    _run(['git', 'commit', '-m', f'chore: register {service} in source-config'], cwd=work)
    _run(['git', 'push', '-u', 'origin', branch], cwd=work)
    pr_url = _run(
        [
            'gh', 'pr', 'create', '--repo', gitops, '--head', branch,
            '--title', f'chore: register {service} in source-config',
            '--body', (
                f'Register `{service}` on {cluster} ({gitops}) so Lighthouse/webhooks pick it up. '
                f'Deterministic edit via gate.tools.source_config — appended to '
                f'`spec.groups[].repositories[]`.'
            ),
        ],
        cwd=work,
    )
    return pr_url.strip()
