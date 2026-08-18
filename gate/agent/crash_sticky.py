"""CLI entry point for posting a crash / cancelled sticky to a PR.

Invoked by the spawned initiative Job's preStop lifecycle hook (D.7) so
operators get a trail in the PR thread when a run is cancelled mid-flight.
The pod is being torn down; we're inside the
``terminationGracePeriodSeconds`` window before SIGKILL, so this script
must be FAST + idempotent + crash-tolerant.

Behaviour:

* ``--repo`` and ``--pr`` are both required-as-flags but tolerate empty
  string values (the hook always passes them, but PR number may be empty
  if the agent never resolved one — exit 0 with a stderr note).
* On any failure (no PR, gh exit non-zero, OSError) we exit 0. The pod is
  shutting down regardless and a non-zero exit from preStop is not
  actionable for the operator.

The actual gh-comment plumbing reuses ``gate.agent.initiative._post_crash_sticky``
and ``_build_crash_sticky_body`` — keeping all crash-sticky shapes in one
place so future changes to the marker / structure land everywhere.
"""

from __future__ import annotations

import sys
from typing import NoReturn

import click

from gate.agent.initiative import _build_crash_sticky_body, _post_crash_sticky

_REASON_TEXT: dict[str, str] = {
    'cancelled': 'cancelled by operator via `POST /initiatives/{id}/cancel`',
    'crashed': 'pod terminated unexpectedly',
}

_HINT_TEXT: dict[str, str] = {
    'cancelled': (
        'The K8s Job was deleted; the pod received SIGTERM and posted this '
        'comment during its `terminationGracePeriodSeconds` grace window. '
        'Any pushed commits remain on the branch — re-fire the initiative '
        'to resume from where the agent left off.'
    ),
    'crashed': (
        'The pod terminated unexpectedly. Substantive work may already be '
        "pushed (this PR's commits). Re-fire is idempotent — the agent "
        'detects the existing branch + PR.'
    ),
}


@click.command()
@click.option('--reason', type=click.Choice(sorted(_REASON_TEXT)), required=True)
@click.option('--repo', type=str, default='', help='qualified repo (owner/name); empty → skip')
@click.option('--pr', type=str, default='', help='PR number; empty → skip')
def main(reason: str, repo: str, pr: str) -> NoReturn:
    """Post a crash / cancelled sticky to the PR, then exit 0.

    Always exits 0 — preStop hooks should never block pod termination
    on a comment post (the pod is shutting down regardless). Surfaces
    progress / failures to stderr so kubectl logs still tells the
    operator what happened.
    """
    repo_stripped = repo.strip()
    pr_stripped = pr.strip()
    if not repo_stripped:
        click.echo('crash_sticky: no repo provided — skipping', err=True)
        sys.exit(0)
    if not pr_stripped:
        click.echo('crash_sticky: no PR number resolved (yet) — skipping', err=True)
        sys.exit(0)

    try:
        pr_number = int(pr_stripped)
    except ValueError:
        click.echo(f'crash_sticky: PR number {pr_stripped!r} is not an int — skipping', err=True)
        sys.exit(0)

    body = _build_crash_sticky_body(
        reason=_REASON_TEXT[reason],
        turn_count=0,
        max_turns=0,
        cost=None,
        hint=_HINT_TEXT[reason],
    )
    _post_crash_sticky(qualified_repo=repo_stripped, pr_number=pr_number, body=body)
    sys.exit(0)


if __name__ == '__main__':
    main()
