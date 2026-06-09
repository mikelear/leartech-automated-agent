"""Introspection helpers for the operator CLI + future dashboard.

The HTTP surface lives in `app/routers/introspection.py`. The functions
here are pure-Python so they're unit-testable without a TestClient and
reusable from CLI/dashboard/scripts.

Modules:
- :mod:`topology` — Mermaid generation for the platform diagram(s).
- :mod:`mcp_status` — Best-effort reachability probes for catalog MCPs.
"""
