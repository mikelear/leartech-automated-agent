"""Terminal, self-explaining repo-access failure — one event name, one destination.

Historical shape
────────────────
``_clone_repo`` in :mod:`gate.agent.initiative` already classified its
own failures — its docstring even named a ``write_failure_reason``
consumer — but the classification reached nothing. The caller
destructured ``(exit_code, failure_reason)`` and threw ``failure_reason``
away. :class:`RunSummary` had no field for it. ``write_failure_reason``
was never implemented anywhere. The run failed correctly with a correct
one-liner computed at the point of failure, and the one-liner
disappeared into the void.

The prod incident that motivated this module: two repos were created by
hand and the operator skipped the ``bootstrap-authed-service``
PlanTemplate step that invites the six machine bots (see
``BotCollaborators`` in
``leartech-mcp-servers/internal/repofactory``). Every subsequent agent
Job pod hit the same ``Repository not found`` on ``git clone``, burned
through its ``backoffLimit`` in 85 seconds across four identical pods,
and the controller rendered ``reason: "Failed"`` with no message on the
Plan CR. The failure was deterministic. The evidence was gone.

Design
──────
Give the existing failure path a destination.

1. **A stable event name**, ``repo_access_denied``, so a single LogQL
   query finds every repo-access failure regardless of where it arose —
   pre-flight, ``git clone``, mid-run ``gh``/``git`` in a Bash-tool
   subprocess. No grepping message text.

2. **Fields on every emission** — ``repo``, ``git_exit_code``,
   ``classification``, ``remediation``. The remediation string points at
   the bootstrap PlanTemplate so the next reader is sent to the fix, not
   left to rediscover it.

3. **Token redaction** is enforced by :func:`redact_token`, applied to
   every stderr snippet emitted. A test asserts the token appears in no
   emitted field. Any refactor that lets the token slip through breaks
   the test.

4. **Read-only pre-flight** via :func:`preflight_declared_repo`, which
   uses ``git ls-remote`` — the same shape ``_remote_branch_exists``
   uses — so it hits no GitHub API and cannot be throttled by the
   5000pts/h GraphQL bucket that made ``gh repo clone`` unusable. A
   PR-open failure that only surfaces write-access denial fires the SAME
   ``repo_access_denied`` event with ``source="midrun"``, so the two
   paths share one query surface.

Write-access probing — the deliberate decision
──────────────────────────────────────────────
Establishing write access WITHOUT mutating the repo is genuinely hard.
GitHub does not expose a "would this push succeed?" endpoint. The only
options are:

* ``git push --dry-run`` — needs a local commit to name in the refspec,
  and the pod's cwd may not even exist at pre-flight time. Also, the
  server-side check is on the receive-pack advertisement, not on any
  particular commit; the "dry" clarifier is client-side only.
* Poke the GraphQL ``viewerPermission`` field — needs a token with
  ``repo`` read + the right OAuth scopes and shares the same 5000pts/h
  bucket the whole point was to stay out of.
* Create a probe branch and delete it — a side effect.

None of those are honest as pre-flight. So this module ONLY probes
READ access, and states that limit explicitly. The mid-run path picks
up the slack: if the first ``git push`` or the first ``gh pr create``
call denies write, the tool result is classified via
:func:`classify_repo_access_failure` and emitted with the SAME event
name. One query — ``event="repo_access_denied"`` — surfaces the
denial regardless of which side of the read/write split hit it.

An honest read-only check beats a write check that leaves a branch
behind, and beats a claim the code cannot support. The PR description
records this decision.

Not covered
───────────
* Multi-repo runs. The pre-flight fires only for the single declared
  ``initiative.primary.qualified_repo``. Mid-run access to referenced
  repos (a nine-repo aggregated run was observed) stays classified but
  not pre-flighted — pre-flighting the undeclarable would defeat the
  reference-chain pattern.
* Repo creation / collaborator invitation. That lives in the
  ``bootstrap-authed-service`` PlanTemplate and the repo-factory MCP's
  ``create_repo`` (see ``BotCollaborators`` in
  ``leartech-mcp-servers/internal/repofactory``). The agent's correct
  move is to verify and refuse; the operator's is to run the bootstrap.
  The remediation string in every emission points there.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from gate import obslog

# Stable event name. Queried directly in LogQL (`event="repo_access_denied"`)
# rather than by grepping message text. Any rename is a documented breaking
# change to the operator-facing signal surface.
EVENT_REPO_ACCESS_DENIED = 'repo_access_denied'

# Sources let one query separate the paths without needing three events.
SOURCE_PREFLIGHT = 'preflight'
SOURCE_CLONE = 'clone'
SOURCE_MIDRUN = 'midrun'

# Classification vocabulary. Kept small and stable so LogQL panels can group by
# it. New classes are added by extending :func:`classify_repo_access_failure`.
CLASS_NO_GH_TOKEN = 'no_gh_token'
CLASS_REPOSITORY_NOT_FOUND = 'repository_not_found'
CLASS_PERMISSION_DENIED = 'permission_denied'
CLASS_NETWORK = 'network'
CLASS_UNREACHABLE = 'unreachable'
CLASS_UNKNOWN = 'unknown_repo_access_failure'

# The remediation string is the DESTINATION for the next reader — where the
# fix lives. Points at the bootstrap PlanTemplate + repo-factory MCP that
# invites the six machine bots. Skipping that step is what caused every
# incident this module fixes. Free-form text on purpose: a URL or path
# change here does not need a schema migration.
REMEDIATION_BOOTSTRAP = (
    'Run the `bootstrap-authed-service` PlanTemplate on this repo (invokes '
    'leartech-mcp-servers repo-factory `create_repo` → invites the six '
    'machine bots via BotCollaborators). The agent runtime deliberately does '
    'not create repos or invite collaborators — verify + refuse is the '
    "correct behaviour here; provisioning is the operator's step."
)

REMEDIATION_GH_TOKEN_UNSET = (
    'Set GH_TOKEN in the pod environment (secret `jx-boot-job-env-vars` or '
    'the equivalent chart value). Without it the git wire protocol cannot '
    'authenticate and no repo can be reached.'
)

REMEDIATION_NETWORK = (
    'Transient network reach failure to github.com. Investigate cluster '
    'egress + DNS; retry manually with `git ls-remote https://github.com/<repo>` '
    'inside the pod. Not a permissions issue.'
)


@dataclass(frozen=True)
class RepoAccessOutcome:
    """Result of a repo-access probe (pre-flight or mid-run classification).

    ``ok`` is the only field a caller decides on; the remaining fields
    are the payload for :func:`emit_repo_access_denied` when ``ok`` is
    False. On success (``ok=True``) every other field is empty / None —
    a green outcome has nothing to say.
    """

    ok: bool
    repo: str = ''
    classification: str = ''
    remediation: str = ''
    git_exit_code: int | None = None
    stderr_snippet: str | None = None
    reason: str = ''  # one-liner suitable for RunSummary.failure_reason
    extra: dict[str, str] = field(default_factory=dict)


def redact_token(text: str, token: str | None) -> str:
    """Return ``text`` with any occurrence of ``token`` replaced by ``***REDACTED***``.

    Called before any stderr snippet is surfaced (echoed, stored in a
    ``failure_reason``, or emitted as a Loki field). Redaction is the
    ONLY protection against a leaky ``fatal: unable to access
    'https://x-access-token:<token>@github.com/...'`` line — git prints
    the URL back at you verbatim on 4xx.

    Returns ``text`` unchanged when the token is falsy (unset / empty) —
    there is nothing to redact and we must not accidentally replace an
    empty-string match. A test asserts the token appears in no emitted
    field even when it is present.
    """
    if not token:
        return text
    return text.replace(token, '***REDACTED***')


def classify_repo_access_failure(
    *,
    stderr: str,
    exit_code: int | None,
) -> tuple[str, str]:
    """Read a git/gh stderr blob → (classification, remediation).

    Called by both :func:`_clone_repo` (via its wrapper) and the mid-run
    Bash-tool result path. Small, stable string patterns matched in the
    order below; unknown text falls back to ``CLASS_UNKNOWN`` with the
    same bootstrap remediation because the incident this module fixes
    was itself an unknown-shape failure until the classifier learned
    ``Repository not found``. Being loud on unknown-shape is the
    correct default.

    Patterns come straight from prod stderr:

    * ``Repository not found`` — git-over-HTTPS when the auth passes
      but the token cannot see the repo (private repo, bot not
      invited). This is the incident shape.
    * ``remote: Permission ... denied`` / ``403``  / ``write access
      ... not granted`` — auth passed, no push perm. Fires on PR-open.
    * ``Could not resolve host`` / ``Temporary failure in name
      resolution`` — network reach, not a permissions issue.
    * ``Connection timed out`` / ``Connection refused`` — likewise.

    The remediation string always points somewhere. The bootstrap
    fallback is correct for the ``repository_not_found`` +
    ``permission_denied`` cases because the collaborator invitation the
    PlanTemplate performs solves both; a network case gets its own
    remediation.
    """
    if not stderr:
        return CLASS_UNKNOWN, REMEDIATION_BOOTSTRAP

    text = stderr.lower()

    if 'repository not found' in text or 'could not resolve to a repository' in text:
        return CLASS_REPOSITORY_NOT_FOUND, REMEDIATION_BOOTSTRAP

    if (
        'could not resolve host' in text
        or 'temporary failure in name resolution' in text
        or 'connection timed out' in text
        or 'connection refused' in text
        or 'network is unreachable' in text
    ):
        return CLASS_NETWORK, REMEDIATION_NETWORK

    permission_markers = (
        'permission denied',
        'write access to repository not granted',
        'the requested url returned error: 403',
        'error: 403',
        'error: 401',
        'authentication failed',
        'bad credentials',
        'gh: not found',
    )
    if any(marker in text for marker in permission_markers):
        return CLASS_PERMISSION_DENIED, REMEDIATION_BOOTSTRAP
    if 'permission to' in text and 'denied' in text:
        return CLASS_PERMISSION_DENIED, REMEDIATION_BOOTSTRAP

    if exit_code is not None and exit_code != 0:
        return CLASS_UNKNOWN, REMEDIATION_BOOTSTRAP

    return CLASS_UNKNOWN, REMEDIATION_BOOTSTRAP


def _last_meaningful_line(text: str) -> str:
    """Return the last non-blank line of ``text``, or the whole thing if none."""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else text


def emit_repo_access_denied(
    *,
    repo: str,
    classification: str,
    remediation: str,
    git_exit_code: int | None,
    stderr_snippet: str | None,
    source: str,
    token: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """Emit ONE ``repo_access_denied`` record. Independent of run_end.

    Why independent: the incident that motivated this module had four
    pods, each of which crashed the caller path BEFORE ``run_end`` fired.
    If the reason only reaches ``run_end`` a pod killed between the
    denial and ``run_end`` loses the only explanation. Emitting at the
    point of failure — with a stable event name — means one Loki query
    still finds every case.

    ``stderr_snippet`` is redacted here (belt-and-braces) — the caller
    is expected to redact too, but this way a caller that forgets does
    not leak the token. The record ONLY carries the fields listed; any
    ``extra`` is dict-flattened onto the emission.
    """
    fields: dict[str, object] = {
        'repo': repo,
        'classification': classification,
        'remediation': remediation,
        'git_exit_code': git_exit_code,
        'source': source,
    }
    if stderr_snippet:
        fields['stderr_snippet'] = redact_token(stderr_snippet, token)
    if extra:
        for key, value in extra.items():
            if isinstance(value, str):
                fields[key] = redact_token(value, token)
            else:
                fields[key] = value
    obslog.emit(
        'ERROR',
        EVENT_REPO_ACCESS_DENIED,
        f'repo access denied: {classification} for {repo} — {remediation}',
        logger='agent.repo_access',
        **fields,
    )


def preflight_declared_repo(
    *,
    qualified_repo: str,
    timeout_seconds: int = 15,
) -> RepoAccessOutcome:
    """Read-only pre-flight of ``qualified_repo`` via ``git ls-remote``.

    Runs BEFORE any model turn. Cost is one HTTPS round-trip and no
    model tokens. The prior incident burned four pods and the Job's
    whole ``backoffLimit`` in 85 seconds re-hitting the same
    deterministic denial across ``max_turns`` of model work; this
    replaces the model-side discovery with a wire-protocol check.

    Same shape as :func:`_remote_branch_exists`:

    * HTTPS + ``x-access-token`` auth. No GitHub API. No GraphQL bucket
      exposure.
    * Short timeout so a wedged network resolves in seconds, not
      minutes.
    * Any ``OSError`` / ``TimeoutExpired`` is caught and turned into an
      ``ok=False`` outcome — a failed probe is a failure signal, not a
      crash.

    Deliberately checks READ ONLY. Write cannot be established without
    a mutating side effect; the mid-run classifier fires the same
    ``repo_access_denied`` event with ``source="midrun"`` when the
    first push or PR-open denies write. This is the "honest read-only
    check beats a write check that leaves a branch behind" decision
    the module docstring lays out.

    Returns a :class:`RepoAccessOutcome`. On ``ok=False`` the caller
    should emit via :func:`emit_repo_access_denied` and return an
    exit-code-2 :class:`RunSummary`. The reason string is suitable for
    :attr:`RunSummary.failure_reason` verbatim.
    """
    gh_token = os.environ.get('GH_TOKEN')
    if not gh_token:
        reason = f'repo_access_denied: GH_TOKEN unset — cannot pre-flight {qualified_repo}'
        return RepoAccessOutcome(
            ok=False,
            repo=qualified_repo,
            classification=CLASS_NO_GH_TOKEN,
            remediation=REMEDIATION_GH_TOKEN_UNSET,
            git_exit_code=None,
            stderr_snippet=None,
            reason=reason,
        )

    url = f'https://x-access-token:{gh_token}@github.com/{qualified_repo}.git'
    # `--heads` alone (no ref pattern) asks the server for every branch ref.
    # A reachable repo returns >=1 line on stdout with exit 0; a 4xx yields
    # non-zero + a "Repository not found" / permission stderr. Passing a
    # pattern like `HEAD` here filters the heads by that name — HEAD is not
    # a branch, so the result was empty-stdout-exit-zero, which our
    # ``ok=True`` check misread as "reachable but useless" every time. The
    # ``_remote_branch_exists`` helper this pre-flight mirrors passes a
    # concrete branch name; for a reachability check we want no filter.
    try:
        result = subprocess.run(
            ['git', 'ls-remote', '--heads', url],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        # No `git` on PATH — that is a build-image gap, not a repo denial.
        # A test env or a stripped image can hit this. Report as
        # `unreachable` so the caller still fails the run (the model
        # cannot work without git), but distinguish it in the reason
        # string.
        return RepoAccessOutcome(
            ok=False,
            repo=qualified_repo,
            classification=CLASS_UNREACHABLE,
            remediation=REMEDIATION_NETWORK,
            git_exit_code=None,
            stderr_snippet=str(exc),
            reason=f'repo_access_denied: unreachable (git not on PATH) for {qualified_repo}',
        )
    except subprocess.TimeoutExpired as exc:
        # A network reach failure is not a permissions failure. Redact any
        # partial stderr the exception carries (git may have echoed the URL).
        raw = ''
        if exc.stderr:
            raw = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        return RepoAccessOutcome(
            ok=False,
            repo=qualified_repo,
            classification=CLASS_UNREACHABLE,
            remediation=REMEDIATION_NETWORK,
            git_exit_code=None,
            stderr_snippet=redact_token(raw, gh_token) if raw else 'ls-remote timed out',
            reason=f'repo_access_denied: unreachable (timeout) for {qualified_repo}',
        )
    except OSError as exc:
        return RepoAccessOutcome(
            ok=False,
            repo=qualified_repo,
            classification=CLASS_UNREACHABLE,
            remediation=REMEDIATION_NETWORK,
            git_exit_code=None,
            stderr_snippet=redact_token(str(exc), gh_token),
            reason=f'repo_access_denied: unreachable (os error) for {qualified_repo}',
        )

    if result.returncode == 0 and result.stdout.strip():
        return RepoAccessOutcome(ok=True, repo=qualified_repo)

    redacted_stderr = redact_token(result.stderr or '', gh_token)
    classification, remediation = classify_repo_access_failure(stderr=redacted_stderr, exit_code=result.returncode)
    snippet = _last_meaningful_line(redacted_stderr) or f'git ls-remote exit {result.returncode}'
    return RepoAccessOutcome(
        ok=False,
        repo=qualified_repo,
        classification=classification,
        remediation=remediation,
        git_exit_code=result.returncode,
        stderr_snippet=snippet[:200],
        reason=(
            f'repo_access_denied: {classification} for {qualified_repo} '
            f'(git ls-remote exit {result.returncode}) — {remediation}'
        ),
    )


# --- Mid-run classification ---------------------------------------------------
#
# When the agent's Bash tool runs `git` or `gh` and the tool result is an
# error, the caller inspects the output for repo-access denial signatures.
# The classifier below is what turns "some Bash exit != 0" into a decision
# to emit `repo_access_denied` — WITHOUT firing on unrelated Bash failures
# (`pytest` red, `ruff` red, etc., which are the vast majority).

# Only try to classify Bash results that came from a git/gh command. Broader
# matches would false-positive on lint output that happens to contain the
# word "permission" or on grep results referencing "Repository not found" in
# a fixture. Keep the command-context check strict.
_REPO_ACCESS_COMMAND_MARKERS: tuple[str, ...] = (
    'git clone',
    'git ls-remote',
    'git fetch',
    'git push',
    'git pull',
    'gh repo',
    'gh pr',
    'gh api',
)


def _command_is_repo_access(command: str) -> bool:
    """Does ``command`` invoke ``git``/``gh`` in a way that could deny repo access?"""
    lowered = command.lower()
    return any(marker in lowered for marker in _REPO_ACCESS_COMMAND_MARKERS)


# Substrings that identify a repo-access failure regardless of exit code.
# We match on the OUTPUT rather than exit code alone because `git push` on a
# missing remote can exit 128 for "unrelated histories" too — the substring
# check gates out that noise.
_REPO_ACCESS_FAILURE_MARKERS: tuple[str, ...] = (
    'repository not found',
    'permission denied',
    'permission to',
    'write access',
    'error: 403',
    'error: 401',
    'the requested url returned error: 403',
    'authentication failed',
    'bad credentials',
    'gh: not found',
    'could not resolve to a repository',
    'could not read from remote repository',
)


def classify_bash_tool_result(
    *,
    command: str,
    output: str,
    is_error: bool,
) -> RepoAccessOutcome:
    """Classify a Bash-tool result — is this a repo-access denial mid-run?

    Returns ``RepoAccessOutcome(ok=True)`` when the command wasn't a
    git/gh call, when the command succeeded, or when the output does
    not carry a known repo-access denial marker. Returns
    ``RepoAccessOutcome(ok=False, ...)`` when it does.

    The caller emits :func:`emit_repo_access_denied` on ``ok=False``
    with ``source="midrun"``, so the mid-run and pre-flight paths land
    under the SAME event name — one LogQL query surfaces every access
    denial regardless of where it arose.

    The token is redacted from the stderr snippet before it lands on
    the outcome. The caller passes the actual token via the emit
    helper's ``token=`` for the belt-and-braces second redaction.
    """
    if not command or not _command_is_repo_access(command):
        return RepoAccessOutcome(ok=True)

    lowered_output = output.lower()
    if not any(marker in lowered_output for marker in _REPO_ACCESS_FAILURE_MARKERS):
        # Bash-tool exit-code failures unrelated to repo access (test red,
        # merge conflicts, etc.) fall through here. Loud on repo access,
        # silent on the rest.
        if not is_error:
            return RepoAccessOutcome(ok=True)
        return RepoAccessOutcome(ok=True)

    gh_token = os.environ.get('GH_TOKEN')
    redacted = redact_token(output, gh_token)
    classification, remediation = classify_repo_access_failure(stderr=redacted, exit_code=None)
    snippet = _last_meaningful_line(redacted) or 'repo access failure marker in output'
    # Try to derive a repo hint from the command text. This is best-effort —
    # the emission still carries a ``repo`` field, defaulting to '' when we
    # cannot parse one out. The Loki query is `event="repo_access_denied"`
    # regardless of whether repo is present.
    return RepoAccessOutcome(
        ok=False,
        repo=_extract_repo_hint(command),
        classification=classification,
        remediation=remediation,
        git_exit_code=None,
        stderr_snippet=snippet[:200],
        reason=(f'repo_access_denied (midrun): {classification} — {remediation}'),
        extra={'command': redact_token(command, gh_token)[:200]},
    )


def _extract_repo_hint(command: str) -> str:
    """Best-effort ``owner/name`` extraction from a git/gh command line.

    Returns '' when nothing plausible is found. The classifier does NOT
    use the extracted value for any decision — it is enrichment for the
    emitted record so an operator scanning Loki sees which repo denied
    access without having to open the pod log.
    """
    for token in command.split():
        if token.count('/') == 1 and not token.startswith('-') and '.' not in token.split('/')[0]:
            candidate = token.rstrip('.git').strip('"\'')
            owner, _, name = candidate.partition('/')
            if owner and name and all(c.isalnum() or c in '-_.' for c in owner + name):
                return candidate
    # Try to pull an owner/name from an HTTPS URL.
    for token in command.split():
        if token.startswith(('http://', 'https://', 'git@')):
            trimmed = token.split('github.com', 1)[-1].lstrip(':/').rstrip('.git').strip('"\'')
            if trimmed.count('/') == 1:
                owner, _, name = trimmed.partition('/')
                if owner and name:
                    return trimmed
    return ''


__all__ = [
    'CLASS_NETWORK',
    'CLASS_NO_GH_TOKEN',
    'CLASS_PERMISSION_DENIED',
    'CLASS_REPOSITORY_NOT_FOUND',
    'CLASS_UNKNOWN',
    'CLASS_UNREACHABLE',
    'EVENT_REPO_ACCESS_DENIED',
    'REMEDIATION_BOOTSTRAP',
    'REMEDIATION_GH_TOKEN_UNSET',
    'REMEDIATION_NETWORK',
    'RepoAccessOutcome',
    'SOURCE_CLONE',
    'SOURCE_MIDRUN',
    'SOURCE_PREFLIGHT',
    'classify_bash_tool_result',
    'classify_repo_access_failure',
    'emit_repo_access_denied',
    'preflight_declared_repo',
    'redact_token',
]
