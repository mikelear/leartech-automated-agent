#!/usr/bin/env python3
"""Mini MCP test client — drive leartech-mcp-servers WITHOUT the release cycle.

Talks raw JSON-RPC over the go-sdk Streamable-HTTP transport (stateless — no
initialize handshake needed; this server accepts tools/list + tools/call
directly). Proves the Python agent can call the real MCP tools instead of its
in-process kubectl/gh shims.

Usage:
  # against a LOCAL server (auth off):
  #   AUTH_REQUIRED=false MCP_DEPLOYMENT_MODE=internal PORT=8899 go run ./cmd/server
  python3 mcp_test_client.py --base http://localhost:8899 --list

  # call a tool:
  python3 mcp_test_client.py --base http://localhost:8899 \
      --call k8s get_pod_state '{"namespace":"jx-staging","name":"..."}'

  # against the DEPLOYED internal MCP (needs a Bearer token + reachability
  # e.g. kubectl port-forward svc/leartech-mcp-servers 8899:80):
  python3 mcp_test_client.py --base http://localhost:8899 --token "$TOKEN" --list
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

SERVERS = [
    'pr_context',
    'tekton',
    'k8s',
    'platform_state',
    'jx3_flow',
    'agent_api',
    'control_plane',
]


def _rpc(base: str, server: str, method: str, params: dict | None, token: str | None) -> dict:
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}).encode()
    req = urllib.request.Request(f'{base}/mcp/{server}', data=body, method='POST')
    req.add_header('content-type', 'application/json')
    req.add_header('accept', 'application/json, text/event-stream')
    if token:
        req.add_header('authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}: {e.read().decode()[:200]}'}
    # Streamable-HTTP may frame the reply as SSE (`event: message\ndata: {...}`)
    # or as plain JSON. Extract the last `data:` line if present.
    if 'data:' in raw:
        for line in raw.splitlines():
            if line.startswith('data:'):
                raw = line[len('data:') :].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {'error': f'unparseable: {raw[:200]}'}


def list_tools(base: str, token: str | None) -> None:
    for s in SERVERS:
        resp = _rpc(base, s, 'tools/list', {}, token)
        tools = resp.get('result', {}).get('tools', [])
        if tools:
            print(f'{s}: ' + ', '.join(t['name'] for t in tools))
        else:
            print(f'{s}: {resp.get("error", "no tools")}')


def call_tool(base: str, server: str, tool: str, args_json: str, token: str | None) -> None:
    args = json.loads(args_json) if args_json else {}
    resp = _rpc(base, server, 'tools/call', {'name': tool, 'arguments': args}, token)
    print(json.dumps(resp.get('result', resp), indent=2)[:2000])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://localhost:8899')
    ap.add_argument('--token', default=None)
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--call', nargs='+', metavar=('SERVER TOOL', 'ARGS_JSON'))
    a = ap.parse_args()
    if a.list:
        list_tools(a.base, a.token)
    if a.call:
        server, tool = a.call[0], a.call[1]
        args_json = a.call[2] if len(a.call) > 2 else '{}'
        call_tool(a.base, server, tool, args_json, a.token)
    if not a.list and not a.call:
        ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
