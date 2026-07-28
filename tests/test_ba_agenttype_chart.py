"""Chart-shape tests for the BA (Business Analyst) AgentType wiring.

Pins the invariants the `feat/ba-agenttype-wiring` initiative introduced
so a future edit doesn't quietly regress:

  * The AgentType manifest exists, is cluster-scoped, and references the
    per-language `leartech-agent-py` image with an entrypoint override that
    runs `gate.agent.ba_agent`.
  * The ExternalSecret template pulls the BA-specific virtual key from the
    right per-cluster sources (GSM key ``agent-ba-gateway-key`` on GCP,
    Vault path ``secret/data/ai/agent-ba-gateway`` property ``apiKey`` on
    AZ) into the neutral secret name ``leartech-ai-gateway-ba-key``.
  * The NetworkPolicy locks the BA pod's egress down to the ai-gateway,
    the internal MCP namespace, kube-apiserver, and kube-dns — nothing
    else. Blanket internet is denied by omission.
  * All three resources are gated on ``.Values.baAgent.registerAgentType``
    so preview releases don't collide on the cluster-scoped resource
    (``AgentType``) or install orphan policies matching a label no pod
    wears in the preview namespace.
  * ``values.yaml`` carries the ``baAgent`` block with default off, the
    secret names + keys required by the initiative, and the internal
    gateway URL.

Chart isn't rendered via `helm template` (no helm CLI in the gate image)
— templates are validated as text-with-Go-templating using substring +
regex assertions. Same approach as `tests/test_chart_initcontainer.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

CHART_ROOT = Path(__file__).parents[1] / 'charts' / 'leartech-automated-agent'
TEMPLATES_DIR = CHART_ROOT / 'templates'
VALUES_YAML = CHART_ROOT / 'values.yaml'

AGENTTYPE_TEMPLATE = TEMPLATES_DIR / 'agenttype-ba.yaml'
EXTERNALSECRET_TEMPLATE = TEMPLATES_DIR / 'externalsecret-ba-gateway-key.yaml'
NETWORKPOLICY_TEMPLATE = TEMPLATES_DIR / 'networkpolicy-ba.yaml'


# ---------------------------------------------------------------------------
# Files exist
# ---------------------------------------------------------------------------


def test_agenttype_template_exists() -> None:
    assert AGENTTYPE_TEMPLATE.exists(), (
        f'{AGENTTYPE_TEMPLATE.name} must exist — the ba-agenttype-wiring '
        'initiative registers BA as a first-class AgentType.'
    )


def test_externalsecret_template_exists() -> None:
    assert EXTERNALSECRET_TEMPLATE.exists(), (
        f'{EXTERNALSECRET_TEMPLATE.name} must exist — the BA agent has '
        'its own gateway virtual key sourced from GSM / Vault / ESO.'
    )


def test_networkpolicy_template_exists() -> None:
    assert NETWORKPOLICY_TEMPLATE.exists(), (
        f'{NETWORKPOLICY_TEMPLATE.name} must exist — the BA pod requires '
        'gateway-only egress. No blanket internet, no arbitrary destinations.'
    )


# ---------------------------------------------------------------------------
# AgentType manifest shape
# ---------------------------------------------------------------------------


def test_agenttype_manifest_is_the_ba_kind_and_name() -> None:
    text = AGENTTYPE_TEMPLATE.read_text()
    assert 'apiVersion: agent.leartech.io/v1alpha1' in text
    assert 'kind: AgentType' in text
    # The initiative explicitly names the AgentType.
    assert re.search(r'^\s*name:\s*leartech-agent-ba\b', text, re.MULTILINE), (
        'AgentType metadata.name must be `leartech-agent-ba` per the initiative.'
    )


def test_agenttype_image_derives_from_agent_py() -> None:
    """The BA runtime reuses the shared per-language `leartech-agent-py`
    image. `leartech-dockerfiles` builds it; the chart composes the URL by
    stripping the API image suffix + appending the agent-py short name.
    The user can override via `.Values.baAgent.image`.
    """
    text = AGENTTYPE_TEMPLATE.read_text()
    # The compose expression: trimSuffix + printf with `/leartech-agent-py:<tag>`.
    assert '/leartech-agent-py' in text, (
        'AgentType.spec.image must reference the leartech-agent-py runtime '
        '(the entrypoint override runs `gate.agent.ba_agent` inside a Python image).'
    )
    # And the .Values.baAgent.image override path exists.
    assert '.Values.baAgent.image' in text


def test_agenttype_entrypoint_runs_ba_agent_module() -> None:
    """Entrypoint override must run `gate.agent.ba_agent`. Mirrors the
    infra_agent pattern (module invocation, not a console script)."""
    text = AGENTTYPE_TEMPLATE.read_text()
    match = re.search(
        r'entrypoint:\s*\n(?:\s*-\s*(?:python|-m|gate\.agent\.ba_agent)\s*\n){3}',
        text,
    )
    assert match, (
        'AgentType.spec.entrypoint must be `[python, -m, gate.agent.ba_agent]` — '
        'the BA agent has no console-script entry, we invoke the module directly.'
    )


def test_agenttype_wires_anthropic_secret_to_ba_key() -> None:
    text = AGENTTYPE_TEMPLATE.read_text()
    assert '.Values.baAgent.secrets.aiGatewayKey.secretName' in text
    assert '.Values.baAgent.secrets.aiGatewayKey.secretKey' in text
    # The CRD's `anthropicSecretName/Key` fields (fixed contract into
    # ANTHROPIC_API_KEY) must reference the BA-specific secret, NOT the
    # shared `leartech-ai-gateway-key`.
    assert 'anthropicSecretName:' in text
    assert 'anthropicSecretKey:' in text


def test_agenttype_env_wires_gateway_url_and_model() -> None:
    text = AGENTTYPE_TEMPLATE.read_text()
    assert 'ANTHROPIC_BASE_URL:' in text
    assert '.Values.baAgent.aiGateway.baseUrl' in text
    assert 'LEARTECH_BA_AGENT_MODEL:' in text
    assert '.Values.baAgent.model' in text


def test_agenttype_gated_on_register_toggle() -> None:
    """Cluster-scoped resources must not collide across previews +
    staging. The manifest renders ONLY when
    `.Values.baAgent.registerAgentType` is explicitly true."""
    text = AGENTTYPE_TEMPLATE.read_text()
    assert re.search(r'\{\{-?\s*if\s+\.Values\.baAgent\.registerAgentType\s*\}\}', text), (
        'AgentType manifest must be gated on .Values.baAgent.registerAgentType'
    )


# ---------------------------------------------------------------------------
# ExternalSecret template shape
# ---------------------------------------------------------------------------


def test_externalsecret_uses_ba_gsm_key_on_gcp() -> None:
    """GSM (raw): key = `agent-ba-gateway-key`."""
    text = EXTERNALSECRET_TEMPLATE.read_text()
    # ExternalSecret is present on the GCP branch.
    assert 'backendType: gcpSecretsManager' in text
    # The gcp.key path (per-cluster override) exists.
    assert '.Values.baAgent.gcp.key' in text


def test_externalsecret_uses_ba_vault_path_on_azure() -> None:
    """Vault KV-v2: path `secret/data/ai/agent-ba-gateway`, property `apiKey`."""
    text = EXTERNALSECRET_TEMPLATE.read_text()
    assert 'backendType: vault' in text
    assert '.Values.baAgent.azure.key' in text
    assert '.Values.baAgent.azure.property' in text


def test_externalsecret_target_key_is_ai_gateway_api_key() -> None:
    """Materialised k8s Secret's key is the provider-neutral
    ``AI_GATEWAY_API_KEY`` — same convention as the shared
    `leartech-ai-gateway-key`. The name is BA-specific:
    ``leartech-ai-gateway-ba-key``.
    """
    text = EXTERNALSECRET_TEMPLATE.read_text()
    assert '.Values.baAgent.secrets.aiGatewayKey.secretKey' in text
    assert '.Values.baAgent.secrets.aiGatewayKey.secretName' in text


def test_externalsecret_gated_on_register_and_backend_toggles() -> None:
    """AND-gated on `.Values.baAgent.registerAgentType`,
    `.Values.baAgent.externalSecret.enabled`, and the cluster's active
    backend (`externalSecrets.{gcp,azure,eso}.enabled`). Previews with
    both toggles off render zero surface."""
    text = EXTERNALSECRET_TEMPLATE.read_text()
    assert re.search(
        r'\{\{-?\s*if and\s+\.Values\.baAgent\.registerAgentType\s+\.Values\.baAgent\.externalSecret\.enabled\s*\}\}',
        text,
    )
    # And each backend block has its own gate — same shape as the shared
    # ai-gateway ExternalSecret template.
    assert '.Values.externalSecrets.gcp.enabled' in text
    assert '.Values.externalSecrets.azure.enabled' in text
    assert '.Values.externalSecrets.eso.enabled' in text


# ---------------------------------------------------------------------------
# NetworkPolicy shape
# ---------------------------------------------------------------------------


def test_networkpolicy_selects_ba_agent_pods_only() -> None:
    text = NETWORKPOLICY_TEMPLATE.read_text()
    match = re.search(
        r'podSelector:\s*\n\s*matchLabels:\s*\n\s*leartech\.io/component:\s*ba-agent',
        text,
    )
    assert match, (
        'NetworkPolicy.podSelector must match `leartech.io/component: ba-agent` — '
        'the same label the controller stamps on BA-agent Job pods.'
    )


def test_networkpolicy_is_egress_only_no_ingress_block() -> None:
    """BA pods are not dialled into by other pods; we control egress + let
    the CNI default deny inbound. Presence of `ingress:` here would be a
    silent widening — assert it's absent."""
    text = NETWORKPOLICY_TEMPLATE.read_text()
    assert re.search(r'^\s*policyTypes:\s*\n\s*-\s*Egress\s*$', text, re.MULTILINE)
    # No top-level `ingress:` key inside the spec.
    assert re.search(r'^\s*ingress:', text, re.MULTILINE) is None, (
        'NetworkPolicy for BA agent must not carry an `ingress:` block — '
        'inbound is CNI-default-deny. Widening this requires an explicit initiative.'
    )


def test_networkpolicy_allows_ai_gateway_egress() -> None:
    text = NETWORKPOLICY_TEMPLATE.read_text()
    assert '.Values.baAgent.networkPolicy.aiGatewayNamespace' in text
    assert '.Values.baAgent.networkPolicy.aiGatewayPort' in text


def test_networkpolicy_allows_mcp_and_kube_apiserver_egress() -> None:
    text = NETWORKPOLICY_TEMPLATE.read_text()
    # Remote MCPs live behind the leartech-mcp Service.
    assert '.Values.baAgent.networkPolicy.mcpNamespace' in text
    assert '.Values.baAgent.networkPolicy.mcpPort' in text
    # kube-apiserver — CIDR-based (Services can't select the endpoint).
    assert '.Values.baAgent.networkPolicy.apiServerCIDR' in text
    # kube-dns — every named egress needs DNS. Both TCP + UDP because
    # clients pick per-query.
    assert 'kubernetes.io/metadata.name: kube-system' in text
    assert 'port: 53' in text


def test_networkpolicy_gated_on_register_and_policy_toggles() -> None:
    text = NETWORKPOLICY_TEMPLATE.read_text()
    assert re.search(
        r'\{\{-?\s*if and\s+\.Values\.baAgent\.registerAgentType\s+\.Values\.baAgent\.networkPolicy\.enabled\s*\}\}',
        text,
    )


# ---------------------------------------------------------------------------
# values.yaml carries the block with the shapes the templates read
# ---------------------------------------------------------------------------


def _values() -> dict:
    return yaml.safe_load(VALUES_YAML.read_text())


def test_values_has_ba_agent_block_off_by_default() -> None:
    """`registerAgentType` is the master toggle — false by default so
    preview releases don't collide on the cluster-scoped AgentType
    resource. The one canonical release per cluster (staging) flips it
    on via GitOps."""
    values = _values()
    assert 'baAgent' in values, 'values.yaml must carry a `baAgent` block'
    assert values['baAgent']['registerAgentType'] is False


def test_values_carries_the_initiative_secret_sources() -> None:
    """GSM key ``agent-ba-gateway-key`` (raw) + Vault path
    ``secret/data/ai/agent-ba-gateway`` property ``apiKey`` — the
    initiative names these explicitly."""
    ba = _values()['baAgent']
    assert ba['gcp']['key'] == 'agent-ba-gateway-key'
    assert ba['azure']['key'] == 'secret/data/ai/agent-ba-gateway'
    assert ba['azure']['property'] == 'apiKey'


def test_values_materialised_secret_is_neutral() -> None:
    """K8s Secret name = `leartech-ai-gateway-ba-key`; key =
    `AI_GATEWAY_API_KEY` (provider-neutral, matches the pattern the
    shared `leartech-ai-gateway-key` uses)."""
    ba = _values()['baAgent']
    assert ba['secrets']['aiGatewayKey']['secretName'] == 'leartech-ai-gateway-ba-key'
    assert ba['secrets']['aiGatewayKey']['secretKey'] == 'AI_GATEWAY_API_KEY'


def test_values_default_gateway_url_is_internal() -> None:
    """The internal ai-gateway URL is hardcoded so every cluster gets
    the same value without a GitOps overlay."""
    ba = _values()['baAgent']
    assert ba['aiGateway']['baseUrl'] == 'http://leartech-ai-gateway.ai-gateway.svc:8080'


def test_values_default_model_is_claude() -> None:
    """`model=claude` = "the gateway's current-best Claude backend"; the
    virtual key's `model_allowlist` resolves to concrete Anthropic ids at
    request time. Initiative pins this default."""
    ba = _values()['baAgent']
    assert ba['model'] == 'claude'


def test_values_networkpolicy_defaults_are_internal_namespaces() -> None:
    ba = _values()['baAgent']
    np = ba['networkPolicy']
    assert np['enabled'] is True
    assert np['aiGatewayNamespace'] == 'ai-gateway'
    assert np['aiGatewayPort'] == 8080
    assert np['mcpNamespace'] == 'leartech-mcp'
    assert np['mcpPort'] == 8080
    # 10.0.0.0/8 covers both GKE (10.0.0.0/14 default) and AKS default
    # service CIDRs. Override per cluster if a non-default CIDR is used.
    assert np['apiServerCIDR'] == '10.0.0.0/8'


def test_values_external_secret_toggle_is_enabled_by_default() -> None:
    """The ExternalSecret template AND-gates on `registerAgentType` +
    `externalSecret.enabled` + the cluster's active backend. So even
    with `externalSecret.enabled: true` (the default here), a preview
    with `registerAgentType: false` still renders zero surface."""
    ba = _values()['baAgent']
    assert ba['externalSecret']['enabled'] is True
