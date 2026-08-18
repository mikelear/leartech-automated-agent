from __future__ import annotations

from gate.agent.initiative import PR_NUMBER_HINT_FILE


def test_pr_number_hint_path_matches_the_path_the_controller_pre_stop_hook_reads() -> None:
    """leartech-orchestrator-controller internal/controller/jobspawn.go injects

        python -m gate.agent.crash_sticky --pr "$(cat /tmp/run_pr_number ...)"

    as the agent Job container's preStop Exec. The literal path is a cross-repo
    contract: changing it here silently stops the hook finding a PR number,
    because the controller swallows the failure with ``|| true``.
    """
    assert PR_NUMBER_HINT_FILE == '/tmp/run_pr_number'  # noqa: S108
