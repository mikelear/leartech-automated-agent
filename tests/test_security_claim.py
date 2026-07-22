"""Unit tests for gate.tools.security_claim — pure parsing + composed verdict.

Mirrors tests/test_chart_overlay.py in shape: sample diffs exercise the pure
parsers; the ``gh``-shelling seam is monkeypatched to cover the I/O branches
without hitting the network.
"""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from gate.tools import security_claim
from gate.tools.security_claim import (
    SecurityClaim,
    evidence_for_claim,
    find_manifest_refs,
    has_in_diff_evidence,
    manifest_exists_at_ref,
    parse_security_claims,
)

# ---------------------------------------------------------------------------
# Sample diffs — the shape of a PR #61-style regression + variations
# ---------------------------------------------------------------------------

# PR #61 shape: a chart comment claiming NetworkPolicy protection, but no
# NetworkPolicy actually added in the diff.
CLAIM_ONLY_NO_EVIDENCE = textwrap.dedent(
    """\
    diff --git a/charts/some-chart/values.yaml b/charts/some-chart/values.yaml
    index 1234567..89abcde 100644
    --- a/charts/some-chart/values.yaml
    +++ b/charts/some-chart/values.yaml
    @@ -10,3 +10,7 @@
     existing: block
    +# The /oauth2/register endpoint is protected by NetworkPolicy in production —
    +# only ingress from the trusted namespace can reach it.
    +hydra:
    +  publicEndpointEnabled: true
    """
)

# Same claim BUT the diff also adds a NetworkPolicy manifest — evidence is
# in-diff.
CLAIM_WITH_INLINE_NETWORKPOLICY = textwrap.dedent(
    """\
    diff --git a/charts/some-chart/values.yaml b/charts/some-chart/values.yaml
    --- a/charts/some-chart/values.yaml
    +++ b/charts/some-chart/values.yaml
    @@ -10,3 +10,5 @@
     existing: block
    +# /oauth2/register is protected by NetworkPolicy in production.
    +publicEndpointEnabled: true
    diff --git a/charts/some-chart/templates/networkpolicy.yaml b/charts/some-chart/templates/networkpolicy.yaml
    new file mode 100644
    --- /dev/null
    +++ b/charts/some-chart/templates/networkpolicy.yaml
    @@ -0,0 +1,10 @@
    +apiVersion: networking.k8s.io/v1
    +kind: NetworkPolicy
    +metadata:
    +  name: hydra-restrict
    +spec:
    +  podSelector:
    +    matchLabels:
    +      app: hydra
    +  policyTypes:
    +    - Ingress
    """
)

# A generic README comment about NetworkPolicy that ISN'T a claim ("we should
# consider adding a NetworkPolicy" is not "this is protected by NetworkPolicy").
COMMENT_ABOUT_NETWORKPOLICY_BUT_NO_CLAIM = textwrap.dedent(
    """\
    diff --git a/README.md b/README.md
    --- a/README.md
    +++ b/README.md
    @@ -1,3 +1,5 @@
     # Some chart
    +We should consider adding a NetworkPolicy someday to lock down ingress.
    +For now, all namespaces can reach the service.
    """
)

# Claim inside a normal Python string literal — NOT a comment, must be
# ignored. This exists to guard against false positives on test fixtures /
# error messages / etc. that mention "protected by NetworkPolicy" as data.
CLAIM_INSIDE_CODE_LITERAL = textwrap.dedent(
    """\
    diff --git a/app/config.py b/app/config.py
    --- a/app/config.py
    +++ b/app/config.py
    @@ -1,3 +1,5 @@
     from foo import bar
    +ERROR_MSG = "This endpoint should be protected by NetworkPolicy in production"
    +CHECK = True
    """
)

# Claim in a code comment referencing an existing manifest path — the
# referenced-manifest branch of evidence resolution takes over.
CLAIM_WITH_REFERENCED_MANIFEST = textwrap.dedent(
    """\
    diff --git a/charts/some-chart/values.yaml b/charts/some-chart/values.yaml
    --- a/charts/some-chart/values.yaml
    +++ b/charts/some-chart/values.yaml
    @@ -10,3 +10,5 @@
     existing: block
    +# /oauth2/register is protected by NetworkPolicy — see
    +# charts/some-chart/templates/networkpolicy.yaml.
    +publicEndpointEnabled: true
    """
)

# Two claim styles in one diff — auth-middleware and RBAC.
CLAIM_AUTH_AND_RBAC = textwrap.dedent(
    """\
    diff --git a/app/routers/admin.py b/app/routers/admin.py
    --- a/app/routers/admin.py
    +++ b/app/routers/admin.py
    @@ -1,3 +1,5 @@
     from fastapi import APIRouter
    +# Admin routes are protected by the auth middleware in production.
    +router = APIRouter()
    diff --git a/charts/some-chart/templates/values.yaml b/charts/some-chart/templates/values.yaml
    --- a/charts/some-chart/templates/values.yaml
    +++ b/charts/some-chart/templates/values.yaml
    @@ -1,3 +1,5 @@
     header: line
    +# Cluster-wide access is gated by RBAC — only cluster-admins can list secrets.
    +key: value
    """
)


# ---------------------------------------------------------------------------
# parse_security_claims
# ---------------------------------------------------------------------------


def test_parse_detects_network_policy_claim_in_yaml_comment() -> None:
    claims = parse_security_claims(CLAIM_ONLY_NO_EVIDENCE)
    assert len(claims) == 1
    c = claims[0]
    assert c.source_file == 'charts/some-chart/values.yaml'
    assert c.claim_type == 'network_policy'
    assert 'protected by NetworkPolicy' in c.claim_snippet
    assert 'oauth2/register' in c.context_line


def test_parse_ignores_documentation_that_is_not_a_claim() -> None:
    """ "We should consider adding X" is NOT the same as "protected by X"."""
    assert parse_security_claims(COMMENT_ABOUT_NETWORKPOLICY_BUT_NO_CLAIM) == []


def test_parse_ignores_claim_inside_code_literal() -> None:
    """A claim string embedded in code (not a comment) must not fire.

    Precision guard — the criterion is about author-authored comments, not
    string data.
    """
    assert parse_security_claims(CLAIM_INSIDE_CODE_LITERAL) == []


def test_parse_detects_multiple_claim_types_across_files() -> None:
    claims = parse_security_claims(CLAIM_AUTH_AND_RBAC)
    types = sorted({c.claim_type for c in claims})
    assert types == ['auth', 'rbac']
    by_type = {c.claim_type: c for c in claims}
    assert by_type['auth'].source_file == 'app/routers/admin.py'
    assert by_type['rbac'].source_file.endswith('values.yaml')


def test_parse_treats_doc_files_as_in_scope_without_comment_prefix() -> None:
    """.md/.rst/.txt content is documentation — no ``#`` needed."""
    diff = textwrap.dedent(
        """\
        diff --git a/docs/security.md b/docs/security.md
        --- a/docs/security.md
        +++ b/docs/security.md
        @@ -1,3 +1,5 @@
         # Security posture
        +The admin API is protected by the auth middleware.
        +Only authenticated users can call it.
        """
    )
    claims = parse_security_claims(diff)
    assert len(claims) == 1
    assert claims[0].claim_type == 'auth'
    assert claims[0].source_file == 'docs/security.md'


def test_parse_ignores_context_and_removed_lines() -> None:
    """Only ``+``-prefixed lines count; context and removed lines are ignored."""
    diff = textwrap.dedent(
        """\
        diff --git a/x.md b/x.md
        --- a/x.md
        +++ b/x.md
        @@ -1,4 +1,4 @@
         The API is protected by NetworkPolicy in production.
        -Older statement here.
        +A new unrelated line.
        """
    )
    # The claim on the context line is NOT a new claim — this PR didn't add it.
    assert parse_security_claims(diff) == []


def test_parse_recognises_alternative_network_policy_phrasings() -> None:
    diff = textwrap.dedent(
        """\
        diff --git a/a.md b/a.md
        --- a/a.md
        +++ b/a.md
        @@ -1,5 +1,8 @@
         intro
        +The endpoint is restricted via NetworkPolicy.
        +The service sits behind a NetworkPolicy.
        +A NetworkPolicy restricts ingress to trusted namespaces.
        """
    )
    claims = parse_security_claims(diff)
    assert len(claims) == 3
    assert {c.claim_type for c in claims} == {'network_policy'}


def test_parse_only_records_first_matching_type_per_line() -> None:
    """A line matching two type patterns still records ONE claim (first-wins).

    Guards against inflated failure output where one confused sentence looks
    like multiple independent claims.
    """
    diff = textwrap.dedent(
        """\
        diff --git a/x.md b/x.md
        --- a/x.md
        +++ b/x.md
        @@ -1,3 +1,4 @@
         intro
        +Protected by NetworkPolicy and protected by RBAC on the same line.
        """
    )
    claims = parse_security_claims(diff)
    assert len(claims) == 1
    assert claims[0].claim_type == 'network_policy'  # first pattern in registry


# ---------------------------------------------------------------------------
# has_in_diff_evidence
# ---------------------------------------------------------------------------


def test_in_diff_evidence_matches_kind_networkpolicy_content() -> None:
    reason = has_in_diff_evidence(CLAIM_WITH_INLINE_NETWORKPOLICY, 'network_policy')
    assert reason
    assert 'networkpolicy.yaml' in reason


def test_in_diff_evidence_matches_networkpolicy_path_even_without_kind_line() -> None:
    """A newly-added file NAMED like a NetworkPolicy is evidence enough — the file's
    header lines aren't guaranteed to appear in the diff for large hunks, so
    the path-shape check is a defensive fallback."""
    diff = textwrap.dedent(
        """\
        diff --git a/charts/x/templates/networkpolicy.yaml b/charts/x/templates/networkpolicy.yaml
        new file mode 100644
        --- /dev/null
        +++ b/charts/x/templates/networkpolicy.yaml
        @@ -0,0 +1,1 @@
        +spec: {}
        """
    )
    reason = has_in_diff_evidence(diff, 'network_policy')
    assert reason
    assert 'path match' in reason


def test_in_diff_evidence_absent_for_unrelated_diff() -> None:
    assert has_in_diff_evidence(CLAIM_ONLY_NO_EVIDENCE, 'network_policy') == ''


def test_in_diff_evidence_matches_auth_middleware_wireup() -> None:
    diff = textwrap.dedent(
        """\
        diff --git a/app/main.py b/app/main.py
        --- a/app/main.py
        +++ b/app/main.py
        @@ -1,3 +1,5 @@
         app = FastAPI()
        +from starlette.middleware.authentication import AuthenticationMiddleware
        +app.add_middleware(AuthenticationMiddleware, backend=MyBackend())
        """
    )
    assert has_in_diff_evidence(diff, 'auth')


def test_in_diff_evidence_matches_auth_dependency_injection() -> None:
    diff = textwrap.dedent(
        """\
        diff --git a/app/routes.py b/app/routes.py
        --- a/app/routes.py
        +++ b/app/routes.py
        @@ -1,3 +1,4 @@
         @router.get('/admin')
        +def admin(user = Depends(get_current_user)):
             ...
        """
    )
    assert has_in_diff_evidence(diff, 'auth')


def test_in_diff_evidence_matches_rbac_clusterrolebinding() -> None:
    diff = textwrap.dedent(
        """\
        diff --git a/charts/x/templates/rbac.yaml b/charts/x/templates/rbac.yaml
        --- a/charts/x/templates/rbac.yaml
        +++ b/charts/x/templates/rbac.yaml
        @@ -1,3 +1,7 @@
        +apiVersion: rbac.authorization.k8s.io/v1
        +kind: ClusterRoleBinding
        +metadata:
        +  name: some-binding
        """
    )
    assert has_in_diff_evidence(diff, 'rbac')


def test_in_diff_evidence_unknown_claim_type_returns_empty() -> None:
    """Unknown claim types are a no-op rather than a crash."""
    assert has_in_diff_evidence(CLAIM_WITH_INLINE_NETWORKPOLICY, 'not_a_real_type') == ''


# ---------------------------------------------------------------------------
# find_manifest_refs
# ---------------------------------------------------------------------------


def test_find_refs_extracts_repo_relative_yaml_path() -> None:
    text = 'See charts/leartech-hydra/templates/networkpolicy.yaml for the guard.'
    assert find_manifest_refs(text) == ['charts/leartech-hydra/templates/networkpolicy.yaml']


def test_find_refs_dedupes_across_sources() -> None:
    title = 'chart(hydra): plumb config (charts/hydra/templates/rbac.yaml)'
    body = 'The guard lives at charts/hydra/templates/rbac.yaml and is wired in.'
    assert find_manifest_refs(title, body) == ['charts/hydra/templates/rbac.yaml']


def test_find_refs_ignores_bare_filename_without_directory() -> None:
    """Requires at least one directory segment — bare "foo.yaml" is ambiguous."""
    assert find_manifest_refs('See networkpolicy.yaml') == []


def test_find_refs_handles_multiple_types() -> None:
    text = 'wired via gate/tools/auth.py, chart at charts/x/templates/rbac.yaml'
    refs = find_manifest_refs(text)
    assert 'gate/tools/auth.py' in refs
    assert 'charts/x/templates/rbac.yaml' in refs


def test_find_refs_empty_on_no_signal() -> None:
    assert find_manifest_refs('title', 'body') == []
    assert find_manifest_refs('', '') == []


# ---------------------------------------------------------------------------
# manifest_exists_at_ref — I/O; every branch folds into a boolean
# ---------------------------------------------------------------------------


def test_manifest_exists_true_on_file_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security_claim, '_gh', lambda args: '"file"\n')
    assert manifest_exists_at_ref('mikelear/foo', 'charts/x/templates/networkpolicy.yaml', 'abc') is True


def test_manifest_exists_true_unquoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh api --jq .type`` sometimes strips quoting depending on version — both accepted."""
    monkeypatch.setattr(security_claim, '_gh', lambda args: 'file\n')
    assert manifest_exists_at_ref('mikelear/foo', 'p', 'abc') is True


def test_manifest_exists_false_on_directory_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory at the path isn't a manifest — the criterion needs a file."""
    monkeypatch.setattr(security_claim, '_gh', lambda args: '"dir"\n')
    assert manifest_exists_at_ref('mikelear/foo', 'p', 'abc') is False


def test_manifest_exists_false_on_gh_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising(args: list[str]) -> str:
        raise RuntimeError('gh api ...: 404 Not Found')

    monkeypatch.setattr(security_claim, '_gh', raising)
    assert manifest_exists_at_ref('mikelear/foo', 'missing.yaml', 'abc') is False


def test_manifest_exists_false_on_gh_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timing_out(args: list[str]) -> str:
        raise RuntimeError('gh api ... timed out after 30s')

    monkeypatch.setattr(security_claim, '_gh', timing_out)
    assert manifest_exists_at_ref('mikelear/foo', 'p', 'abc') is False


# ---------------------------------------------------------------------------
# _gh — subprocess wrapper's error paths (mirrors chart_overlay's tests)
# ---------------------------------------------------------------------------


def test_gh_raises_runtime_error_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCompleted:
        returncode = 1
        stdout = ''
        stderr = '404 Not Found\n'

    def fake_run(cmd: list[str], **kwargs: object) -> FakeCompleted:
        return FakeCompleted()

    monkeypatch.setattr(security_claim.subprocess, 'run', fake_run)
    with pytest.raises(RuntimeError, match='404 Not Found'):
        security_claim._gh(['api', 'anything'])


def test_gh_raises_runtime_error_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(security_claim.subprocess, 'run', fake_run)
    with pytest.raises(RuntimeError, match='timed out'):
        security_claim._gh(['api', 'anything'])


# ---------------------------------------------------------------------------
# evidence_for_claim — composed verdict
# ---------------------------------------------------------------------------


def _make_claim(
    claim_type: str = 'network_policy',
    source_file: str = 'charts/some-chart/values.yaml',
    context_line: str = '# /oauth2/register is protected by NetworkPolicy in production.',
    claim_snippet: str = 'protected by NetworkPolicy',
) -> SecurityClaim:
    return SecurityClaim(
        source_file=source_file,
        claim_type=claim_type,
        claim_snippet=claim_snippet,
        context_line=context_line,
    )


def test_evidence_ok_when_diff_introduces_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy in-diff path — network policy manifest in the same PR."""
    # Force manifest_exists_at_ref to False so we know the pass comes from in-diff.
    monkeypatch.setattr(security_claim, 'manifest_exists_at_ref', lambda *a, **kw: False)
    claim = _make_claim()
    ok, reason = evidence_for_claim(
        claim,
        diff=CLAIM_WITH_INLINE_NETWORKPOLICY,
        pr_title='',
        pr_body='',
        repo='mikelear/some-chart',
        head_sha='deadbeefcafebabe',
    )
    assert ok
    assert 'guard present in diff' in reason


def test_evidence_ok_when_referenced_manifest_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """No in-diff evidence, but the comment cites a manifest that exists at head."""
    claim = _make_claim(
        context_line=(
            '# protected by NetworkPolicy — see charts/some-chart/templates/networkpolicy.yaml for the guard.'
        ),
    )
    lookups: list[tuple[str, str, str]] = []

    def fake_lookup(repo: str, path: str, ref: str = 'main') -> bool:
        lookups.append((repo, path, ref))
        return path.endswith('networkpolicy.yaml')

    monkeypatch.setattr(security_claim, 'manifest_exists_at_ref', fake_lookup)
    ok, reason = evidence_for_claim(
        claim,
        diff=CLAIM_ONLY_NO_EVIDENCE,
        pr_title='irrelevant',
        pr_body='irrelevant',
        repo='mikelear/some-chart',
        head_sha='deadbeef',
    )
    assert ok
    assert 'referenced manifest' in reason
    assert 'networkpolicy.yaml' in reason
    assert lookups  # we actually did the existence check


def test_evidence_ok_when_pr_body_references_existing_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reference can live in PR body, not just the comment itself."""
    monkeypatch.setattr(security_claim, 'manifest_exists_at_ref', lambda *a, **kw: True)
    claim = _make_claim()
    ok, reason = evidence_for_claim(
        claim,
        diff=CLAIM_ONLY_NO_EVIDENCE,
        pr_title='chart: enable /oauth2/register',
        pr_body=(
            'The endpoint is guarded by charts/some-chart/templates/networkpolicy.yaml which already exists on main.'
        ),
        repo='mikelear/some-chart',
        head_sha='deadbeef',
    )
    assert ok
    assert 'referenced manifest' in reason


def test_evidence_fails_when_pr61_style_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #61 shape: claim in comment, no guard in diff, no manifest reference."""
    monkeypatch.setattr(security_claim, 'manifest_exists_at_ref', lambda *a, **kw: False)
    claim = _make_claim()
    ok, reason = evidence_for_claim(
        claim,
        diff=CLAIM_ONLY_NO_EVIDENCE,
        pr_title='chart: enable /oauth2/register',
        pr_body='trust me it works',
        repo='mikelear/some-chart',
        head_sha='deadbeefcafebabe',
    )
    assert not ok
    assert 'no matching guard in the diff' in reason
    assert 'no manifest' in reason
    assert 'network_policy' in reason


def test_evidence_fails_when_referenced_manifest_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claim cites a manifest, but that manifest doesn't exist at head SHA."""
    monkeypatch.setattr(security_claim, 'manifest_exists_at_ref', lambda *a, **kw: False)
    claim = _make_claim(
        context_line=('# protected by NetworkPolicy — see charts/some-chart/templates/networkpolicy.yaml'),
    )
    ok, reason = evidence_for_claim(
        claim,
        diff=CLAIM_ONLY_NO_EVIDENCE,
        pr_title='',
        pr_body='',
        repo='mikelear/some-chart',
        head_sha='deadbeefcafebabe',
    )
    assert not ok
    assert 'but none exist at head SHA' in reason
    assert 'deadbee' in reason  # short SHA appears in the failure message


def test_evidence_composed_verdict_prefers_in_diff_over_referenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When BOTH in-diff evidence AND a referenced manifest exist, the in-diff path
    wins for reporting purposes — it's the stronger, self-contained proof."""
    calls: list[tuple[str, str, str]] = []

    def track_lookup(repo: str, path: str, ref: str = 'main') -> bool:
        calls.append((repo, path, ref))
        return True

    monkeypatch.setattr(security_claim, 'manifest_exists_at_ref', track_lookup)
    claim = _make_claim(
        context_line=('# protected by NetworkPolicy — see charts/x/templates/networkpolicy.yaml'),
    )
    ok, reason = evidence_for_claim(
        claim,
        diff=CLAIM_WITH_INLINE_NETWORKPOLICY,
        pr_title='',
        pr_body='',
        repo='mikelear/foo',
        head_sha='deadbeef',
    )
    assert ok
    assert 'guard present in diff' in reason
    # In-diff verdict short-circuits — the manifest-existence lookup mustn't fire.
    assert calls == []
