"""leartech-criteria-mcp — discover and run gate criteria.

Runs criteria as a subprocess (`uv run gate check ...`) rather than calling pytest in-process,
because pytest mutates global state that's awkward to compose inside a long-lived agent loop.
The structured `.report.json` is parsed and returned.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

CRITERIA_ROOT = Path(__file__).parent.parent / 'criteria'


def _list_criteria_files() -> dict[str, list[str]]:
    """Walk the criteria directory tree and collect test_* function names per file."""
    out: dict[str, list[str]] = {}
    for path in sorted(CRITERIA_ROOT.rglob('test_*.py')):
        rel = str(path.relative_to(CRITERIA_ROOT))
        funcs: list[str] = []
        for line in path.read_text().splitlines():
            if line.startswith('def test_') or line.startswith('async def test_'):
                # `def test_foo(...)` — extract up to '('
                name = line.split('def ', 1)[1].split('(', 1)[0]
                funcs.append(name)
        if funcs:
            out[rel] = funcs
    return out


@tool(
    'list_criteria',
    'List every available criterion grouped by file. Returns a map of '
    '"<criteria-file-relpath>" → [test function names]. Use this to learn what '
    'the gate covers before running it.',
    {},
)
async def _list_criteria(_args: dict[str, Any]) -> dict[str, Any]:
    payload = _list_criteria_files()
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'run_criteria_set',
    'Run the gate against a real PR. Returns the structured pytest-json-report summary: '
    '{"passed": N, "failed": N, "skipped": N, "tests": [{name, outcome, message}]}. '
    'Optional `mark` filter (e.g. shared|unit|integration|e2e|playwright) limits to one tier.',
    {'repo': str, 'pr_number': int, 'mark': str},
)
async def _run_criteria_set(args: dict[str, Any]) -> dict[str, Any]:
    repo = str(args['repo'])
    pr_number = int(args['pr_number'])
    mark = str(args.get('mark') or '')

    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as tmp:
        report_path = Path(tmp.name)

    cmd = [
        'uv',
        'run',
        'pytest',
        str(CRITERIA_ROOT),
        '--repo',
        repo,
        '--pr',
        str(pr_number),
        '--json-report',
        f'--json-report-file={report_path}',
        '--tb=no',
        '-q',
    ]
    if mark:
        cmd.extend(['-m', mark])

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if not report_path.exists():
        return {
            'content': [
                {
                    'type': 'text',
                    'text': json.dumps(
                        {
                            'error': 'pytest did not produce a report',
                            'returncode': result.returncode,
                            'stdout': result.stdout[-2000:],
                            'stderr': result.stderr[-2000:],
                        },
                        indent=2,
                    ),
                }
            ]
        }

    raw = json.loads(report_path.read_text())
    report_path.unlink(missing_ok=True)

    summary = raw.get('summary', {})
    tests = []
    for t in raw.get('tests', []):
        message = ''
        call = t.get('call') or {}
        if call.get('outcome') == 'failed':
            longrepr = call.get('longrepr') or ''
            # Take the trailing AssertionError line if present, else the last non-empty line.
            for line in reversed(str(longrepr).splitlines()):
                if line.strip():
                    message = line.strip()
                    break
        elif t.get('outcome') == 'skipped':
            setup = t.get('setup') or {}
            longrepr = setup.get('longrepr') or call.get('longrepr') or ''
            text = str(longrepr)
            if 'Skipped:' in text:
                message = text.split('Skipped:', 1)[1].splitlines()[0].strip().lstrip('"').rstrip("'")
        tests.append(
            {
                'name': t.get('nodeid', ''),
                'outcome': t.get('outcome', ''),
                'message': message,
            }
        )

    payload = {
        'returncode': result.returncode,
        'summary': {
            'passed': summary.get('passed', 0),
            'failed': summary.get('failed', 0),
            'skipped': summary.get('skipped', 0),
            'total': summary.get('total', 0),
        },
        'tests': tests,
    }
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


def build_criteria_server() -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name='leartech-criteria',
        version='0.1.0',
        tools=[_list_criteria, _run_criteria_set],
    )
