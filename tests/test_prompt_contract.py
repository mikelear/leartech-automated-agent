"""The prompt is a contract with the code, and CI holds it.

Prompt text is not covered by any other test, so it rots silently: an MCP server can be
deleted while the prompt still instructs the agent to call its tools, and the agent
burns turns discovering that. It also invited host-shaped instructions — the prompt told
the agent to read logs via a shell script under a developer's home directory, which does
not exist in the Job container.

The LLM routing around a broken instruction is not a pass: it costs turns and tokens, and
it keeps the dead code that instruction refers to alive.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator

import pytest

from gate.agent.initiative import INITIATIVE_TEKTON_TOOLS, WRITE_MODE_TOOLS
from gate.agent.main import MCP_ALLOWED_TOOLS
from gate.agent.mcp_catalog import load_catalog
from scripts.render_system_prompt import assemble

MCP_TOOL_RE = re.compile(r'mcp__(?P<server>[a-z0-9-]+)__(?P<tool>[a-z0-9_]+)')

ALLOWED_TOOLS = {*WRITE_MODE_TOOLS, *MCP_ALLOWED_TOOLS, *INITIATIVE_TEKTON_TOOLS}

# Committed snapshot of the live MCP tool schemas, produced by
# ``scripts/snapshot_mcp_tool_schemas.py --write``. The tests below assert
# the prompt against THIS file — never over the network — so a laptop can
# run pytest without minting a token and CI never silently reports success
# because credentials happened to be missing.
_SCHEMAS_PATH = pathlib.Path(__file__).parent.parent / 'docs' / 'mcp-tool-schemas.json'

# Type tokens the prompt is allowed to use for each JSON-schema primitive.
# The prompt is prose, not code, so "integer" and "int" both count. Keep
# this list tight — "number" is deliberately NOT a match for "integer"
# because the schema distinguishes them (Go int vs float64).
_PROMPT_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    'integer': ('integer', 'int'),
    'string': ('string', 'str'),
    'boolean': ('boolean', 'bool'),
    'number': ('number', 'float'),
    'array': ('array', 'list'),
    'object': ('object', 'dict'),
}


def _prompt_text() -> str:
    return assemble('initiative_agent')


def _load_schemas() -> dict[str, dict[str, dict[str, object]]]:
    """Load the committed MCP tool-schema snapshot.

    Raises if the file is missing or malformed — a missing snapshot means
    the operator has not run ``scripts/snapshot_mcp_tool_schemas.py --write``
    yet, and the whole contract collapses to "nothing to check against".
    """
    loaded: dict[str, dict[str, dict[str, object]]] = json.loads(_SCHEMAS_PATH.read_text())
    return loaded


def _iter_tool_call_signatures(text: str) -> Iterator[tuple[str, str, list[str]]]:
    """Yield ``(server, tool, args)`` for every ``mcp__server__tool(...)`` call in ``text``.

    ``args`` is the list of argument names (from ``name`` or ``name=value``) with
    ``<...>`` placeholders stripped, so ``step_status(pipelinerun_name=<foo>, cluster=<bar>)``
    yields ``['pipelinerun_name', 'cluster']``. The call site pattern in the prompt
    is deliberately narrow: fully-qualified ``mcp__…__…(...)`` with parentheses.
    Backticked or bare mentions (``wait_for_terminal`` alone) do not signal an
    argument list; those are covered by the name-only assertion elsewhere.
    """
    pattern = re.compile(r'mcp__(?P<server>[a-z0-9-]+)__(?P<tool>[a-z0-9_]+)\((?P<args>[^)]*)\)')
    for match in pattern.finditer(text):
        raw = match.group('args').strip()
        if not raw:
            yield match.group('server'), match.group('tool'), []
            continue
        args: list[str] = []
        for part in raw.split(','):
            token = part.strip()
            if not token:
                continue
            name = token.split('=', 1)[0].strip()
            # ``name=<placeholder>`` and ``<placeholder>``-only cases both strip cleanly.
            name = name.split('<', 1)[0].strip()
            if name:
                args.append(name)
        yield match.group('server'), match.group('tool'), args


def _iter_arg_type_claims(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(argument_name, stated_type)`` for every ```arg` is a[n] TYPE`` clause.

    The prompt states types in prose: "The PR argument is `pr_number` (an INTEGER".
    Rather than parse every English phrasing, we look for the specific pattern the
    prompt uses today — "`arg_name` … (?:is|are|as|—) an? TYPE" — because a broader
    NLP net would either miss real claims (false negative → the type check never
    fires) or invent claims from prose ("pr_number is a load-bearing arg" ≠ a type).

    Concretely we match ``pr_number`` framed by prompt words that connect a name to
    a type: ``INTEGER``, ``STRING``, ``BOOLEAN``, ``NUMBER``. Case is normalised on
    output; the prompt writes them capitalised for emphasis.
    """
    # Only pick up type words rendered in ALL CAPS or with a leading article —
    # the prompt uses "INTEGER" / "an INTEGER" for emphasis, which is exactly
    # the shape of a type claim we want to hold to the schema. Non-emphatic
    # occurrences ("the integer part of the timestamp") are not claims about a
    # tool arg.
    pattern = re.compile(
        r'`(?P<arg>[a-z_][a-z0-9_]*)`[^`]{0,80}?(?:is|are)\s+(?:an?\s+)?(?P<type>INTEGER|STRING|BOOLEAN|NUMBER)',
    )
    for match in pattern.finditer(text):
        yield match.group('arg'), match.group('type').lower()


def _schema_property_types(
    schemas: dict[str, dict[str, dict[str, object]]],
    server: str,
    tool: str,
    arg: str,
) -> set[str] | None:
    """Return the set of JSON-Schema types accepted for ``arg``, or ``None`` when absent.

    Handles the three shapes the MCP host emits:

      * plain string type — ``{"type": "integer"}`` → ``{"integer"}``
      * union type — ``{"type": ["null", "array"]}`` → ``{"array"}``. ``null`` is
        dropped because a nullable X is still "X" from the prompt's perspective.
      * ``oneOf`` — ``{"oneOf": [{"type": "integer"}, {"type": "string"}]}``. The
        pr_number arg uses this to accept both int and digit-string. Each branch's
        ``type`` is unioned into the return set, so the prompt saying either
        one of those types matches.

    Returning ``None`` (property absent) is distinct from returning ``set()``
    (property present but with no type — the "any" case). Callers use ``None`` to
    fall through to the tool_signature-arguments test.
    """
    server_key = f'leartech-{server}' if not server.startswith('leartech-') else server
    tool_entry = schemas.get(server_key, {}).get(tool)
    if not isinstance(tool_entry, dict):
        return None
    input_schema = tool_entry.get('inputSchema')
    if not isinstance(input_schema, dict):
        return None
    props = input_schema.get('properties')
    if not isinstance(props, dict):
        return None
    prop = props.get(arg)
    if not isinstance(prop, dict):
        return None
    types: set[str] = set()
    prop_type = prop.get('type')
    if isinstance(prop_type, str):
        types.add(prop_type)
    elif isinstance(prop_type, list):
        for candidate in prop_type:
            if isinstance(candidate, str) and candidate != 'null':
                types.add(candidate)
    one_of = prop.get('oneOf')
    if isinstance(one_of, list):
        for branch in one_of:
            if isinstance(branch, dict):
                branch_type = branch.get('type')
                if isinstance(branch_type, str):
                    types.add(branch_type)
    return types


def test_every_mcp_tool_named_in_the_prompt_is_actually_wired() -> None:
    """A tool the prompt names but the agent cannot call is a turn wasted on a 404."""
    load_catalog.cache_clear()
    catalog = load_catalog()
    catalogued = {name.replace('leartech-', '', 1) if False else name for name in catalog.mcp_servers}

    unknown_servers: set[str] = set()
    unwired_tools: set[str] = set()
    for match in MCP_TOOL_RE.finditer(_prompt_text()):
        server = (
            f'leartech-{match.group("server")}'
            if not match.group('server').startswith('leartech-')
            else match.group('server')
        )
        full = match.group(0)
        if server not in catalogued and match.group('server') not in catalogued:
            unknown_servers.add(match.group('server'))
        elif full not in ALLOWED_TOOLS:
            unwired_tools.add(full)

    assert not unknown_servers, (
        f'the prompt names MCP servers that are not in the catalog: {sorted(unknown_servers)} — '
        'either the server was deleted and the prompt was not updated, or the catalog is missing it'
    )
    assert not unwired_tools, (
        f'the prompt names MCP tools the agent is not allowed to call: {sorted(unwired_tools)} — '
        'add them to allowed_tools or stop instructing the agent to use them'
    )


def test_prompt_contains_no_host_specific_paths() -> None:
    """The agent runs in a Job container, not on anyone's laptop.

    Repo-relative paths (``scripts/e2e.sh``) are fine — they live in the checkout the
    agent is working on. What is not fine is a path rooted in a developer's home
    directory, which the container has no equivalent of.
    """
    offenders = [pattern for pattern in ('~/', '/Users/', '/home/') if pattern in _prompt_text()]
    assert not offenders, (
        f'the prompt references host-shaped paths {offenders} — the agent runs in a Job '
        'container where they do not exist, so it burns turns failing before routing around it'
    )


def test_rendered_prompt_snapshot_is_current() -> None:
    """docs/agent-system-prompt.md must match what the code assembles.

    The prompt is built from three sources — the JX3 calibration, the lessons catalog and
    the Python-rendered role prompt — so editing any one of them changes agent behaviour
    with no readable diff. Committing the assembled text means a reviewer sees the change
    in the PR that causes it, including changes made by adding a lesson file.
    """
    result = subprocess.run(
        [sys.executable, 'scripts/render_system_prompt.py'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_prompt_references_no_python_module_that_no_longer_exists() -> None:
    """The prompt must not cite `gate.<module>` paths that have been deleted.

    Two survived the last cut — `gate.mcp_servers.pipeline_server` and
    `gate.tools.e2e_coverage` — because the tool-name and path rules above do not look at
    module paths. An instruction naming a module that is gone sends the agent looking for
    code that cannot be found, which costs turns and reads as though the code still exists.
    """
    import pathlib

    existing: set[str] = {'gate'}
    for path in pathlib.Path('gate').rglob('*.py'):
        if '__pycache__' in str(path):
            continue
        dotted = str(path)[:-3].replace('/', '.').replace('.__init__', '')
        parts = dotted.split('.')
        for i in range(1, len(parts) + 1):
            existing.add('.'.join(parts[:i]))

    referenced = set(re.findall(r'\bgate(?:\.[a-z_]+)+', _prompt_text()))
    dead = sorted(name for name in referenced if name not in existing)
    assert not dead, (
        f'the prompt cites Python modules that do not exist: {dead} — '
        'either the module was deleted and the prompt was not updated, or the path is wrong'
    )


def test_mcp_tool_signatures_use_the_schema_argument_names() -> None:
    """Where the prompt spells out a tool call, the argument names must be the real ones.

    On the controller-ba-agent-default-sa run the agent called
    wait_for_first_failure_or_all_pass with `pr` instead of `pr_number` and the MCP rejected
    it — "unexpected additional properties [\"pr\"]" — costing a turn before it corrected
    itself. The prompt named arguments for cancel_superseded_for_pr but not for the wait
    tools, so it was guessing from the JSON schema.

    leartech-mcp-servers spells the PR argument `pr_number` on every tool that takes one
    (WaitForTerminalIn, WaitForFirstFailureOrAllPassIn, and the tekton tools).
    """
    text = _prompt_text()

    bad = re.findall(r'(mcp__[a-z0-9_-]+__[a-z_]+)\(([^)]*)\)', text)
    offenders = [
        f'{tool}({args})' for tool, args in bad if re.search(r'\bpr\b\s*(?:,|$)', args) and 'pr_number' not in args
    ]
    assert not offenders, f'tool signatures name the PR argument `pr`, but the schema calls it `pr_number`: {offenders}'

    for tool in ('wait_for_first_failure_or_all_pass', 'wait_for_terminal'):
        assert re.search(rf'{tool}\(repo, pr_number', text), (
            f'{tool} should have its arguments spelled out in the prompt — the agent '
            'otherwise guesses them from the schema and can burn a turn on a rejection'
        )


# ---------------------------------------------------------------------------
# Snapshot-backed contract: prompt tool signatures vs. live MCP schemas.
#
# The five tests below make ``docs/mcp-tool-schemas.json`` load-bearing. Each
# test doubles as an executable statement of what "the prompt matches the
# schema" MEANS: name coverage, argument coverage, type agreement, snapshot
# integrity, and end-to-end pass on the current text. Break any one and the
# suite goes red — proved out in
# ``test_prompt_contract_new_assertions_are_load_bearing`` below.
# ---------------------------------------------------------------------------


def test_snapshot_artefact_is_well_formed() -> None:
    """``docs/mcp-tool-schemas.json`` must exist and describe every MCP the prompt uses.

    A missing / truncated / renamed snapshot means the assertions in the tests below
    would silently pass — they would find no schema to check against and every
    lookup would fall through to "no such tool", which is only a real signal when
    the file itself is trustworthy.
    """
    assert _SCHEMAS_PATH.exists(), (
        f'{_SCHEMAS_PATH.relative_to(_SCHEMAS_PATH.parent.parent)} is missing — '
        'run: python scripts/snapshot_mcp_tool_schemas.py --write'
    )
    schemas = _load_schemas()
    # These are the MCPs the initiative agent's prompt actually names. Extending
    # this list is a deliberate choice — see `scripts/snapshot_mcp_tool_schemas.py`.
    for server in ('leartech-jx3-flow', 'leartech-pr-context', 'leartech-tekton'):
        assert server in schemas, f'snapshot is missing MCP {server!r} — regenerate it'
        assert schemas[server], f'snapshot lists MCP {server!r} but with no tools — regenerate it'
        for tool_name, entry in schemas[server].items():
            assert 'inputSchema' in entry, f'{server}.{tool_name} missing inputSchema in snapshot'


def test_prompt_tool_names_exist_in_snapshot() -> None:
    """Every ``mcp__server__tool`` reference in the prompt must resolve to a snapshot tool.

    Motivating case: an MCP server is renamed or a tool is removed in leartech-mcp-servers,
    but the prompt still tells the agent to call it — the agent burns a turn discovering
    the tool is gone (or worse, discovers it silently and picks a nearby name). The name
    check the existing catalog-only test does can't catch this: a name may match the
    catalog and still be missing from the published tools list.
    """
    schemas = _load_schemas()
    missing: list[str] = []
    for match in MCP_TOOL_RE.finditer(_prompt_text()):
        server = match.group('server')
        tool = match.group('tool')
        server_key = f'leartech-{server}' if not server.startswith('leartech-') else server
        if tool not in schemas.get(server_key, {}):
            missing.append(f'{server_key}.{tool}')
    assert not missing, (
        f'the prompt names MCP tools that are not in the snapshot: {sorted(set(missing))} — '
        'either the tool was removed / renamed and the prompt was not updated, or the '
        'snapshot is stale (run: python scripts/snapshot_mcp_tool_schemas.py --write)'
    )


def test_prompt_tool_signature_arguments_exist_in_schema() -> None:
    """Every argument in a spelled-out signature must appear in the tool's inputSchema.

    The motivating case here was ``step_status(pipelinerun=…)``: the prompt named an
    argument the schema doesn't recognise (it wants ``pipelinerun_name``), so any
    time the agent copied the signature verbatim the tool rejected the call with
    "unexpected additional properties". The name-only check three tests up can't
    catch this — the TOOL name matches; only an ARG name is wrong.
    """
    schemas = _load_schemas()
    offenders: list[str] = []
    for server, tool, args in _iter_tool_call_signatures(_prompt_text()):
        server_key = f'leartech-{server}' if not server.startswith('leartech-') else server
        tool_entry = schemas.get(server_key, {}).get(tool)
        if not isinstance(tool_entry, dict):
            continue  # covered by test_prompt_tool_names_exist_in_snapshot
        input_schema = tool_entry.get('inputSchema')
        if not isinstance(input_schema, dict):
            continue
        properties = input_schema.get('properties', {})
        if not isinstance(properties, dict):
            continue
        for arg in args:
            if arg not in properties:
                offenders.append(f'{server_key}.{tool}({arg}) — schema properties: {sorted(properties)}')
    assert not offenders, (
        'the prompt spells out tool arguments the schema does not recognise: '
        f'{offenders} — the MCP will reject the call as "unexpected additional properties"'
    )


def test_prompt_type_claims_match_schema() -> None:
    """Where the prompt states an argument's type, the schema must agree.

    "``pr_number`` is an INTEGER" is a claim about the wire contract. If someone
    later widens the schema to accept a string, the prompt is now lying and this
    test catches it before the agent does at run time. Conversely, if someone
    changes the prompt to say "STRING", the schema disagrees and this test
    catches THAT.

    The scan uses ``_iter_arg_type_claims`` which only matches ALL-CAPS type
    words (INTEGER / STRING / BOOLEAN / NUMBER) — the prompt uses them
    emphatically. A non-emphatic mention ("the integer part of the timestamp")
    is not a type claim about a tool arg.
    """
    schemas = _load_schemas()
    text = _prompt_text()
    # Find every tool that names each arg somewhere in a signature, so a stated
    # type "``pr_number`` is INTEGER" is checked against every tool that takes
    # pr_number — a schema drift on ANY of them is a bug.
    arg_to_tools: dict[str, list[tuple[str, str]]] = {}
    for server, tool, args in _iter_tool_call_signatures(text):
        for arg in args:
            arg_to_tools.setdefault(arg, []).append((server, tool))
    mismatches: list[str] = []
    for arg, stated_type in _iter_arg_type_claims(text):
        aliases = set(_PROMPT_TYPE_ALIASES.get(stated_type, (stated_type,)))
        for server, tool in arg_to_tools.get(arg, []):
            actual = _schema_property_types(schemas, server, tool, arg)
            if actual is None:
                continue  # covered by test_prompt_tool_signature_arguments_exist_in_schema
            if not actual:
                # schema advertises no explicit type — nothing to disagree with
                continue
            # A stated type MATCHES the schema when at least one of its aliases
            # appears in the accepted set. For pr_number's ``oneOf: [integer,
            # digit-string]`` this means both "INTEGER" and "STRING" prompt
            # claims validate; only a truly unaccepted type (BOOLEAN, NUMBER)
            # trips the mismatch. That's the schema's real contract.
            if aliases.isdisjoint(actual):
                mismatches.append(
                    f'prompt says `{arg}` is {stated_type!r} but '
                    f'leartech-{server}.{tool} schema accepts types {sorted(actual)!r}'
                )
    assert not mismatches, (
        'prompt states a type that disagrees with the MCP schema: '
        f'{mismatches} — the tool will reject the wrong type at run time'
    )


def test_pr_number_is_documented_as_an_integer_in_the_prompt() -> None:
    """The motivating case: ``pr_number`` MUST carry a type annotation.

    Symptom (the initiative goal): the agent has passed ``pr_number`` as a string
    three times across two runs, on two different tools. The prompt names the arg
    correctly but never says the type, and the model's guess costs a turn. The
    fix is to state the type in the prompt AND enforce it here — a duplicate that
    is tested is better than one that isn't, and better than removing the guidance
    (which invites the string-name regression that motivated the ``pr_number``
    name clause in the first place).

    ``test_prompt_type_claims_match_schema`` above then verifies the stated type
    matches the schema, so this test's role is narrower: guarantee the type
    claim is PRESENT so the schema-agreement test has something to check.
    """
    text = _prompt_text()
    claims = [(arg, t) for arg, t in _iter_arg_type_claims(text) if arg == 'pr_number']
    assert claims, (
        'the prompt names `pr_number` in tool signatures but never states its type — '
        'the agent has passed a string three times across two runs. Add a clause like '
        '"pr_number is an INTEGER" so the model does not have to infer it, and so '
        'test_prompt_type_claims_match_schema can hold the claim to the schema.'
    )
    assert all(t == 'integer' for _, t in claims), (
        f'the prompt states `pr_number` as {claims!r}, but the schema requires integer'
    )


def test_prompt_contract_new_assertions_are_load_bearing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove each new assertion actually catches its failure mode.

    A test that passes by accident (the property it asserts is never violated) is
    worse than no test — it advertises coverage that does not exist. This test
    breaks each contract in turn (via ``monkeypatch``) and confirms the
    corresponding assertion fires. If it doesn't, the assertion is dead weight
    and this test surfaces that.

    Runs in an isolated tmp_path with monkeypatched sources so the real
    snapshot / real prompt are never mutated.
    """
    real_schemas = _load_schemas()
    real_prompt = _prompt_text()

    def _use_schemas(schemas: dict[str, dict[str, dict[str, object]]]) -> None:
        path = tmp_path / 'mcp-tool-schemas.json'
        path.write_text(json.dumps(schemas))
        monkeypatch.setattr('tests.test_prompt_contract._SCHEMAS_PATH', path)

    def _use_prompt(text: str) -> None:
        # ``_prompt_text`` calls ``assemble`` bound in THIS module's namespace,
        # so we patch the local reference; patching ``render_system_prompt.assemble``
        # is ineffective once the ``from … import assemble`` has already bound the
        # symbol here.
        monkeypatch.setattr('tests.test_prompt_contract.assemble', lambda role: text)

    # 1. Prompt naming a tool absent from the snapshot fails.
    _use_schemas({'leartech-jx3-flow': {}, 'leartech-pr-context': {}, 'leartech-tekton': {}})
    _use_prompt(real_prompt)
    with pytest.raises(AssertionError, match='not in the snapshot'):
        test_prompt_tool_names_exist_in_snapshot()

    # 2. Prompt naming an argument absent from the tool fails.
    truncated_schemas = json.loads(json.dumps(real_schemas))
    step_status = truncated_schemas['leartech-tekton']['step_status']['inputSchema']
    step_status['properties'] = {k: v for k, v in step_status['properties'].items() if k != 'pipelinerun_name'}
    step_status['required'] = [k for k in step_status['required'] if k != 'pipelinerun_name']
    _use_schemas(truncated_schemas)
    _use_prompt(real_prompt)
    with pytest.raises(AssertionError, match='unexpected additional properties'):
        test_prompt_tool_signature_arguments_exist_in_schema()

    # 3. Prompt stating the wrong type fails.
    # pr_number's schema is oneOf[integer, digit-string] — a coercion added
    # explicitly to be forgiving. So both INTEGER and STRING pass the match
    # check. A truly unaccepted type — BOOLEAN — is what surfaces the
    # mismatch, and would be a real bug if the prompt ever claimed it.
    _use_schemas(real_schemas)
    _use_prompt(real_prompt.replace('`pr_number` (an INTEGER', '`pr_number` is a BOOLEAN (an INTEGER'))
    with pytest.raises(AssertionError, match='disagrees with the MCP schema'):
        test_prompt_type_claims_match_schema()

    # 4. Stale snapshot (missing an entire MCP the prompt uses) fails the
    #    structural check. Include real tools for the two present MCPs so the
    #    "MCP present but empty" branch doesn't trip first — this test proves
    #    the "missing MCP" branch specifically.
    _use_prompt(real_prompt)
    _use_schemas(
        {
            'leartech-jx3-flow': real_schemas['leartech-jx3-flow'],
            'leartech-pr-context': real_schemas['leartech-pr-context'],
        }
    )  # missing leartech-tekton
    with pytest.raises(AssertionError, match='missing MCP'):
        test_snapshot_artefact_is_well_formed()

    # 5. Removing the pr_number type clause fails the motivating-case guard.
    _use_schemas(real_schemas)
    _use_prompt(re.sub(r'`pr_number`[^`]{0,80}?INTEGER', '`pr_number`', real_prompt))
    with pytest.raises(AssertionError, match='never states its type'):
        test_pr_number_is_documented_as_an_integer_in_the_prompt()

    # 6. And the current prompt+snapshot pair passes ALL five (this is what CI runs).
    monkeypatch.undo()
    test_snapshot_artefact_is_well_formed()
    test_prompt_tool_names_exist_in_snapshot()
    test_prompt_tool_signature_arguments_exist_in_schema()
    test_prompt_type_claims_match_schema()
    test_pr_number_is_documented_as_an_integer_in_the_prompt()
