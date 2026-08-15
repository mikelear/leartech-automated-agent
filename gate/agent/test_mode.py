"""TEST-MODE for the shared agent entrypoint.

The dev-agent (`gate.agent.initiative`), the infra agent
(`gate.agent.infra_agent`), the BA agent (`gate.agent.ba_agent`), and the
read-only review agent (`gate.agent.main`) all share the same run-path shape:
read inputs, drive the Claude Agent SDK loop, exit with a status.  This module
adds a **short-circuit BEFORE the SDK loop** that a plan step can request via
``inputs.testMode``:

- ``testMode.finishAs``   — ``"Succeeded"`` or ``"Failed"`` — the phase to
                            self-report on the ``AgentRun.status``.
- ``testMode.prOutcome``  — ``merged``/``closed``/``open``/``awaiting``/``none``.
                            When the step is PR-backed AND ``prOutcome != none``
                            the agent STILL calls the real ``open_pr`` MCP tool
                            (which is where our PR-capture bugs have historically
                            lived) — the tool's own test-mode support (in
                            leartech-mcp-servers) returns a synthetic PR and
                            patches ``AgentRun.status.targetPR`` as normal.
- ``testMode.message``    — optional free-text placed on ``status.message``.
- ``testMode.delaySeconds`` — optional non-negative delay before self-reporting
                              (lets a plan test the "sit Running for N seconds"
                              path before terminal).

CRITICAL SAFETY GUARD — the ``LEARTECH_AGENT_TEST_MODE_ALLOWED`` env var must be
set to ``"true"`` (case-insensitive) for testMode to have any effect.  When
that env is unset OR any other value, the agent MUST ignore ``inputs.testMode``
entirely and run normally.  This makes it structurally impossible for a stray
testMode directive in a production plan to accidentally no-op a real run.  The
chart defaults the env OFF; only test-shaped deployments turn it on.

Provider-portability note (see AI-GATEWAY-AND-PORTABILITY.md): the test-mode
path is DELIBERATELY LLM-free — no ``anthropic``, no ``claude_agent_sdk``
imports.  Skipping the reasoning loop is the whole point (so tests exercise
downstream orchestration without burning tokens).  The ``open_pr`` invocation
uses standard client-side MCP (via ``streamablehttp_client``) — the same
transport the stdio bridge uses.  A future non-Anthropic runtime keeps the
test-mode contract intact.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ── Public constants ────────────────────────────────────────────────────────

#: Environment variable that gates test-mode.  Absent / any-other-value = OFF.
#: The chart's default MUST leave this unset; only test-shaped deployments
#: (staging fixture pods, e2e clusters) turn it on.
TEST_MODE_GUARD_ENV = 'LEARTECH_AGENT_TEST_MODE_ALLOWED'

#: The annotation key stamped on the AgentRun when a test-mode run fires.
#: External tools (dashboard filters, forensic queries) use this to distinguish
#: test-mode runs from real ones.  The value is the string ``"true"``.
TEST_MODE_ANNOTATION_KEY = 'leartech.io/test-mode'
TEST_MODE_ANNOTATION_VALUE = 'true'

#: Kubernetes AgentRun CR coordinates (mirror ``agentrun_client.py``).
_GROUP = 'agent.leartech.io'
_VERSION = 'v1alpha1'
_AGENTRUNS = 'agentruns'

#: The one MCP server we may need to reach from test-mode — pr_context (open_pr).
_PR_CONTEXT_SERVER = 'pr_context'
_OPEN_PR_TOOL = 'open_pr'


FinishAs = Literal['Succeeded', 'Failed']
PrOutcome = Literal['merged', 'closed', 'open', 'awaiting', 'none']


class TestModeSpec(BaseModel):
    """The parsed ``inputs.testMode`` contract.

    ``extra='forbid'`` because a typo in the plan step (``finish_as`` instead of
    ``finishAs``) should raise loudly rather than silently no-op — that's how
    the "stray directive skips a real run" foot-gun happens.
    """

    # Suppresses the pytest "cannot collect test class ..." warning — the class
    # name starts with ``Test`` but it's a pydantic model, not a test case.
    __test__ = False

    # ``populate_by_name=True`` so tests + Python callers can construct with
    # snake_case (``finish_as=...``) while the plan-step contract on the wire
    # stays the mixedCase JSON idiom (``finishAs``). ``extra='forbid'`` keeps
    # a typo (``finish_as`` in the YAML) from silently no-op'ing.
    model_config = ConfigDict(extra='forbid', populate_by_name=True)

    finish_as: FinishAs = Field(
        alias='finishAs',
        description=(
            'The AgentRun.status.phase to self-report before exiting. '
            "'Succeeded' produces exit code 0; 'Failed' produces 1."
        ),
    )
    pr_outcome: PrOutcome = Field(
        default='none',
        alias='prOutcome',
        description=(
            "How the plan step wants a PR-backed run's PR to look. "
            "'none' skips the open_pr call entirely (non-PR-backed steps use this). "
            "Any other value causes a real open_pr invocation — the MCP tool's own "
            'test-mode support (in leartech-mcp-servers) shapes the synthetic PR.'
        ),
    )
    message: str | None = Field(
        default=None,
        description='Optional human-readable message placed on AgentRun.status.message.',
    )
    delay_seconds: int | None = Field(
        default=None,
        alias='delaySeconds',
        ge=0,
        description=(
            'Optional non-negative delay before the terminal self-report — '
            'lets a plan test the "sit Running for N seconds" path.'
        ),
    )


# ── Guard + parser ──────────────────────────────────────────────────────────


def is_test_mode_allowed(env: dict[str, str] | None = None) -> bool:
    """Return True iff the guard env is set to ``"true"`` (case-insensitive).

    ``env`` is injectable for tests — production callers pass ``None`` and read
    from ``os.environ``.  Anything other than a case-insensitive ``"true"``
    (empty, ``"false"``, ``"1"``, ``"yes"``, garbage) reads as ``False`` — we
    deliberately do NOT accept the various "truthy" spellings so this env
    behaves like a strict boolean, not a permissive flag.  A permissive parse
    is exactly the kind of accident that would let a typo (``LEARTECH_AGENT_TEST_MODE_ALLOWED=turue``)
    quietly turn test-mode on in production.
    """
    e = env if env is not None else os.environ
    raw = (e.get(TEST_MODE_GUARD_ENV) or '').strip().lower()
    return raw == 'true'


def parse_test_mode(
    inputs: dict[str, Any] | None,
    *,
    env: dict[str, str] | None = None,
) -> TestModeSpec | None:
    """Return the parsed TestModeSpec iff BOTH the guard is set AND inputs.testMode is present.

    Returns ``None`` when:
      - the guard env is unset or not ``"true"`` — testMode is IGNORED even
        when the inputs carry it (this is the safety invariant that lets a
        real run survive a stray testMode directive), OR
      - ``inputs`` is ``None`` / not a dict, OR
      - ``inputs`` lacks a ``testMode`` key, OR
      - ``inputs.testMode`` is not a dict.

    Raises ``pydantic.ValidationError`` when the guard is on AND testMode IS
    present but structurally invalid — we want that to fail loudly so a
    malformed testMode block in a test plan is a hard error, not a silent skip.
    Callers wrap the call in ``try/except`` if they want to degrade gracefully;
    the four entrypoint modules deliberately do NOT so a broken testMode block
    is caught at the earliest opportunity.
    """
    if not is_test_mode_allowed(env):
        return None
    if not isinstance(inputs, dict):
        return None
    raw = inputs.get('testMode')
    if not isinstance(raw, dict):
        return None
    return TestModeSpec.model_validate(raw)


# ── AgentRun status-patch path ──────────────────────────────────────────────
#
# The existing status-patch path uses the two env vars every controller-spawned
# agent pod has:
#
#   AGENT_RUN_NAMESPACE   the namespace the AgentRun CR lives in (from the Job's env)
#   LEARTECH_RUN_ID       the AgentRun's metadata.name (same as the K8s Job's LEARTECH_RUN_ID)
#
# We patch two subresources:
#   1. metadata (annotation stamp): standard patch_namespaced_custom_object
#   2. status.phase + status.message: patch via the /status subresource so we
#      don't bump metadata.generation (which would confuse the controller into
#      thinking the spec had changed).
#
# Both are merge-patch (RFC 7396) so we only overwrite the fields we specify;
# in particular ``status.targetPR`` (previously set by the open_pr MCP tool)
# survives the phase update, matching the "keep the targetPR from open_pr"
# requirement in the initiative goal.


async def stamp_test_mode_annotation(*, name: str, namespace: str) -> bool:
    """Merge-patch ``metadata.annotations[leartech.io/test-mode]=true`` on the AgentRun.

    Best-effort: returns True on success, False on any failure (RBAC denied,
    404, transport error).  Test-mode runs are diagnostic; a failed annotation
    stamp does not change the run's exit outcome.  The failure is logged so
    the operator can spot a chart mis-configuration (RBAC not granting
    ``patch`` on ``agentruns``) rather than mystery-debug an "annotation didn't
    appear" symptom.
    """
    try:
        from kubernetes_asyncio import client, config
    except ImportError:  # pragma: no cover — kubernetes_asyncio is a prod dep
        logger.warning('test-mode: kubernetes_asyncio not importable — cannot stamp annotation')
        return False

    try:
        config.load_incluster_config()
    except Exception as exc:  # noqa: BLE001 — laptop runs never have in-cluster config
        logger.info('test-mode: not in-cluster (%s) — annotation stamp skipped', exc)
        return False

    api = client.CustomObjectsApi(client.ApiClient())
    body = {
        'metadata': {
            'annotations': {TEST_MODE_ANNOTATION_KEY: TEST_MODE_ANNOTATION_VALUE},
        },
    }
    try:
        await api.patch_namespaced_custom_object(
            group=_GROUP,
            version=_VERSION,
            namespace=namespace,
            plural=_AGENTRUNS,
            name=name,
            body=body,
            _content_type='application/merge-patch+json',
        )
        logger.info(
            'test-mode: stamped %s=%s on agentrun %s/%s',
            TEST_MODE_ANNOTATION_KEY,
            TEST_MODE_ANNOTATION_VALUE,
            namespace,
            name,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort observability, never crash the loop
        logger.warning('test-mode: annotation stamp failed for %s/%s: %s', namespace, name, exc)
        return False
    finally:
        await api.api_client.close()


async def patch_agentrun_phase(
    *,
    name: str,
    namespace: str,
    phase: str,
    message: str | None = None,
) -> bool:
    """Merge-patch AgentRun.status via the /status subresource.

    Sets ``.status.phase = <phase>`` and, when ``message`` is non-empty,
    ``.status.message = <message>``.  Every other status field (``targetPR``,
    ``completionTime``, ``iteration``, …) is untouched — merge-patch RFC 7396
    only overwrites fields we specify.  That's the "keep the targetPR from
    open_pr" guarantee in the initiative goal.

    Uses the ``/status`` subresource (``patch_namespaced_custom_object_status``)
    rather than a top-level patch so ``metadata.generation`` is NOT bumped by
    the change — bumping generation would signal to the controller that spec
    changed, triggering a re-reconcile.  Status subresource semantics avoid
    that; only the ``.status`` view mutates.

    Returns True on success, False on any failure.  A failed patch is logged
    at WARN level so operators can diagnose RBAC / API-server issues from the
    pod log; the caller's exit code still reflects the test-mode intent
    (Succeeded → 0, Failed → 1) so a broken status-patch doesn't mask the
    test's declared outcome — the outcome is the exit code, the status
    subresource is the observability surface.
    """
    try:
        from kubernetes_asyncio import client, config
    except ImportError:  # pragma: no cover
        logger.warning('test-mode: kubernetes_asyncio not importable — cannot patch status')
        return False

    try:
        config.load_incluster_config()
    except Exception as exc:  # noqa: BLE001
        logger.info('test-mode: not in-cluster (%s) — status patch skipped', exc)
        return False

    api = client.CustomObjectsApi(client.ApiClient())
    status: dict[str, Any] = {'phase': phase}
    if message:
        status['message'] = message
    body = {'status': status}
    try:
        await api.patch_namespaced_custom_object_status(
            group=_GROUP,
            version=_VERSION,
            namespace=namespace,
            plural=_AGENTRUNS,
            name=name,
            body=body,
            _content_type='application/merge-patch+json',
        )
        logger.info(
            'test-mode: patched agentrun %s/%s status.phase=%s (message=%r)',
            namespace,
            name,
            phase,
            message,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            'test-mode: status patch failed for %s/%s (phase=%s): %s',
            namespace,
            name,
            phase,
            exc,
        )
        return False
    finally:
        await api.api_client.close()


# ── open_pr MCP invocation from test-mode ───────────────────────────────────


async def call_open_pr_via_mcp(open_pr_args: dict[str, Any]) -> bool:
    """Call the real ``open_pr`` MCP tool directly, without going through the SDK.

    Uses the same discovery + auth path the ``build_remote_mcp_servers``
    wiring uses:

      1. ``LEARTECH_MCP_URL`` — the internal MCP host base.
      2. ``mint_mcp_token`` — mints a fresh aud=leartech-mcp bearer.
      3. ``discover_mounts`` — finds the pr_context server's mount path.
      4. Opens an authed streamable-HTTP client + ``ClientSession``.
      5. Calls ``open_pr(**open_pr_args)``.

    The MCP tool's own test-mode support (implemented in the separate
    leartech-mcp-servers repo) handles the "return a synthetic PR + still
    patch AgentRun.status.targetPR" behaviour — from our side we just have to
    call it.  This is the HISTORICALLY-BUGGY code path (PR-capture missing
    number, wrong-PR mis-capture, gh pr create fallback strand) so exercising
    it end-to-end in test-mode is the whole point.

    Returns True on success (tool returned isError=False), False on any
    failure.  Failures are logged at WARN so operators can diagnose the
    open_pr call independently of the phase self-report.

    Provider-portability: we deliberately DON'T import from claude_agent_sdk.
    The transport is standard client-side MCP (``streamablehttp_client``) —
    the exact seam that survives a switch to a non-Anthropic runtime.
    """
    # Import here (not at module top) so test-mode's *module* is importable
    # in environments without the mcp package installed (e.g. unit tests that
    # never exercise this code path).  The imports are cheap on first use and
    # cached by Python thereafter.
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:  # pragma: no cover — mcp is a prod dep
        logger.warning('test-mode: mcp package not importable (%s) — open_pr call skipped', exc)
        return False

    from gate.mcp_servers.remote import discover_mounts, mint_mcp_token

    base = (os.environ.get('LEARTECH_MCP_URL') or '').rstrip('/')
    if not base:
        logger.warning('test-mode: LEARTECH_MCP_URL unset — cannot call open_pr')
        return False

    token = mint_mcp_token()
    if not token:
        logger.warning('test-mode: could not mint aud=leartech-mcp token — open_pr call skipped')
        return False

    mounts = discover_mounts(base, token)
    if not mounts or _PR_CONTEXT_SERVER not in mounts:
        logger.warning(
            'test-mode: pr_context MCP not advertised by %s/mcps — open_pr call skipped',
            base,
        )
        return False

    url = f'{base}{mounts[_PR_CONTEXT_SERVER]}'
    headers = {'Authorization': f'Bearer {token}'}
    try:
        async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _sid):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(_OPEN_PR_TOOL, open_pr_args)
    except Exception as exc:  # noqa: BLE001 — network / auth failures are all "call failed"
        logger.warning('test-mode: open_pr transport failed against %s: %s', url, exc)
        return False

    if getattr(result, 'isError', False):
        # Extract a compact text summary from the CallToolResult for the log —
        # the exact shape depends on the MCP tool's error content.  We only
        # need a diagnostic snippet, not the full payload.
        snippet = _summarise_call_result(result)
        logger.warning('test-mode: open_pr returned isError=True: %s', snippet)
        return False

    logger.info('test-mode: open_pr call succeeded (synthetic PR published; args=%s)', open_pr_args)
    return True


def _summarise_call_result(result: Any) -> str:
    """Best-effort compact rendering of a CallToolResult's content for logs."""
    try:
        content = getattr(result, 'content', None) or []
        parts: list[str] = []
        for item in content:
            text = getattr(item, 'text', None)
            if text:
                parts.append(str(text))
        return ' | '.join(parts) or repr(result)
    except Exception:  # noqa: BLE001 — logging helper must never raise
        return repr(result)


# ── Top-level test-mode runner ──────────────────────────────────────────────


async def run_test_mode(
    spec: TestModeSpec,
    *,
    open_pr_args: dict[str, Any] | None = None,
    sleep: Any = asyncio.sleep,
) -> int:
    """Execute the test-mode directive end-to-end. Returns the process exit code.

    Sequence (matches the initiative goal spec):

      1. Stamp the ``leartech.io/test-mode=true`` annotation on the AgentRun.
      2. If ``spec.delaySeconds`` is set, sleep for that many seconds — this
         is the "sit Running for N seconds first" behaviour that lets a plan
         test in-flight status views before terminal.
      3. If ``spec.prOutcome != 'none'`` AND ``open_pr_args`` is provided,
         call the real ``open_pr`` MCP tool.  The tool's own test-mode
         support in leartech-mcp-servers publishes a synthetic PR and patches
         ``AgentRun.status.targetPR`` — we just have to invoke it.
      4. Self-report the terminal phase via the AgentRun /status subresource
         (``spec.finishAs`` → ``phase``, optional ``spec.message`` → ``message``).
      5. Return 0 for ``Succeeded`` / 1 for ``Failed``.

    All k8s / MCP calls are best-effort: a failure to reach the API server is
    logged but does NOT change the exit code — the exit code MUST reflect the
    plan's declared intent so upstream orchestration sees a consistent signal
    regardless of transient infra hiccups.

    ``sleep`` is injectable (kwarg default is ``asyncio.sleep``) so unit tests
    can pass a fake to observe the delay without waiting real seconds.
    """
    logger.info(
        'test-mode ACTIVE — finishAs=%s prOutcome=%s message=%r delaySeconds=%s',
        spec.finish_as,
        spec.pr_outcome,
        spec.message,
        spec.delay_seconds,
    )

    # Identity via the captured snapshot rather than :data:`os.environ` —
    # ``gate.agent.initiative.run_initiative`` STRIPS ``AGENT_RUN_NAMESPACE``
    # from the env at startup so subprocesses can't reach the AgentRun,
    # but this same-process code still legitimately needs the handle. See
    # :mod:`gate.identity` for the design.
    from gate import identity  # local import: keep test_mode importable in tools without gate.identity resolved

    run_id = identity.get_run_id()
    namespace = identity.get_namespace()
    can_patch_agentrun = bool(run_id) and bool(namespace)
    if not can_patch_agentrun:
        # Post-strip (sanitise-subprocess-identity) the strict "should never
        # happen in a controller-spawned Job" reading no longer holds: a
        # test-mode invocation firing from a subprocess (e.g. a Bash-tool
        # ``uv run pytest`` inside the agent's own gate suite) sees an
        # EXPECTED empty identity because the parent stripped it. The
        # message is now honest about both paths.
        logger.warning(
            'test-mode: run_id (%r) / namespace (%r) unavailable — AgentRun '
            'annotation + status patches will be skipped. This is EXPECTED '
            'on a laptop invocation, or in a subprocess whose parent stripped '
            'the identity env (see gate.identity). It should NOT happen at '
            'the top of a controller-spawned Job before any strip has fired.',
            run_id,
            namespace,
        )

    # 1. Annotation stamp — before anything else so ANY subsequent failure
    #    still leaves the "this was test-mode" breadcrumb on the AgentRun.
    if can_patch_agentrun:
        await stamp_test_mode_annotation(name=run_id, namespace=namespace)

    # 2. Optional delay — lets a test observe the "sit Running" window.
    if spec.delay_seconds:
        logger.info('test-mode: sleeping %ss before terminal phase report', spec.delay_seconds)
        await sleep(spec.delay_seconds)

    # 3. PR-backed step — still call the REAL open_pr MCP tool. This is the
    #    historically-buggy code path (targetPR mis-capture, gh-pr-create
    #    fallback strand, wrong-PR attribution) so exercising it in test-mode
    #    is the whole point.
    if spec.pr_outcome != 'none' and open_pr_args:
        await call_open_pr_via_mcp(open_pr_args)
    elif spec.pr_outcome != 'none' and not open_pr_args:
        logger.info(
            'test-mode: prOutcome=%s but no open_pr_args provided (non-PR-backed step) — skipping open_pr call',
            spec.pr_outcome,
        )

    # 4. Self-report the terminal phase (keeps targetPR from step 3 intact
    #    because merge-patch only touches the fields we specify).
    if can_patch_agentrun:
        await patch_agentrun_phase(
            name=run_id,
            namespace=namespace,
            phase=spec.finish_as,
            message=spec.message,
        )

    # 5. Exit code — Succeeded → 0, Failed → 1. This is the ONE thing that
    #    ALWAYS reflects the plan's declared intent, regardless of any
    #    k8s / MCP hiccup above.
    return 0 if spec.finish_as == 'Succeeded' else 1


# ── Convenience for entrypoints ─────────────────────────────────────────────


def maybe_open_pr_args_for_initiative(
    *,
    qualified_repo: str,
    base_branch: str,
    head_branch: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Build the ``open_pr`` MCP tool's argument dict for the dev-agent path.

    The dev-agent (``gate.agent.initiative``) is the only entrypoint that
    genuinely OPENS a PR as part of its run.  Other entrypoints (infra_agent,
    ba_agent, main.py) either open no PR or open a repo-factory-shaped one
    with its own arg shape.  This helper centralises the initiative-shaped
    argument dict so both the real path (assembled by the LLM in the SDK
    loop) and the test-mode path (assembled here) produce the same shape.

    The controller-spawned Job already carries LEARTECH_RUN_ID +
    AGENT_RUN_NAMESPACE, so the tool call publishes the synthetic PR against
    THIS run — matching the real path's behaviour.

    Identity via the captured snapshot: ``gate.agent.initiative.run_initiative``
    strips these vars from :data:`os.environ` at startup, but this helper
    still needs the values to build a well-formed ``open_pr`` request.
    See :mod:`gate.identity` for the design.
    """
    from gate import identity  # local import: matches ``run_test_mode``'s pattern

    run_id = identity.get_run_id()
    namespace = identity.get_namespace()
    return {
        'run_id': run_id,
        'namespace': namespace,
        'repo': qualified_repo,
        'base': base_branch,
        'head': head_branch,
        'title': title,
        'body': body,
    }


__all__ = [
    'FinishAs',
    'PrOutcome',
    'TEST_MODE_ANNOTATION_KEY',
    'TEST_MODE_ANNOTATION_VALUE',
    'TEST_MODE_GUARD_ENV',
    'TestModeSpec',
    'call_open_pr_via_mcp',
    'is_test_mode_allowed',
    'maybe_open_pr_args_for_initiative',
    'parse_test_mode',
    'patch_agentrun_phase',
    'run_test_mode',
    'stamp_test_mode_annotation',
]
