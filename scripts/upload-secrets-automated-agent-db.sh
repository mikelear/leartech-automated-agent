#!/bin/bash
# Upload the workload DSN secret for leartech-automated-agent to GSM (GCP)
# + Vault (AZ). PRINTS COMMANDS — copy and run them manually.
#
# Scope
#   This script only writes the DSN that the workload reads. The role +
#   role-password are owned by leartech-platform-postgres (auth-ui session)
#   and live in:
#     GCP GSM:  cnpg-automated-agent-password
#     AZ Vault: secret/automated-agent/cnpg-automated-agent-password
#                 (property: password)
#   This script READS that role password and embeds it in the DSN string,
#   then writes the DSN to the workload-side store the chart's ExternalSecret
#   reads from:
#     GCP GSM:  automated-agent-db-dsn  (flat string)
#     AZ Vault: secret/automated-agent/automated-agent-db
#                 (property: dsn)
#
# Why this script exists
#   Standard libpq DSN spelling (?sslmode=require) trips asyncpg at runtime
#   (TypeError: connect() got an unexpected keyword argument 'sslmode').
#   The chart's app/db/_normalise_dsn translates it server-side from PR #27
#   onward — so the libpq form is the canonical one to write here. Keep
#   sslmode=require for parity with psql/IDE clients; the code handles it.
#
# Run order
#   1. auth-ui session merges + deploys leartech-platform-postgres role for
#      leartech_automated_agent (this is a prerequisite).
#   2. Verify the role password ExternalSecret on each cluster is SUCCESS:
#        kubectl -n cnpg-system get externalsecret leartech-automated-agent-credentials
#   3. Run this script for the cluster(s) you're bootstrapping.
#   4. Verify the workload-side Secret materialises:
#        kubectl -n jx-staging get secret leartech-automated-agent-db
#   5. Restart deploy/leartech-automated-agent to pick up the DSN (or wait
#      for next rollout if a chart change is already in flight).
#
# Cluster topology
#   leartech-staging-rw is the CNPG read-write service in the cnpg-system
#   namespace, reachable from jx-staging via cluster DNS:
#     leartech-staging-rw.cnpg-system.svc.cluster.local:5432
#   Same DNS resolves on both GCP and AZ clusters — the DSN string is
#   identical, only the password (and the secret store) differ per cluster.

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

section() {
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  $1${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

cmd() {
    echo -e "${CYAN}$1${NC}"
}

note() {
    echo -e "${YELLOW}# $1${NC}"
}

echo "=== leartech-automated-agent — DSN upload (GCP + AZ) ==="
echo ""
echo "This script prints commands to upload the workload DSN. Copy each block"
echo "into a shell that has the prerequisites set up (kubectl context, gcloud"
echo "auth, Vault port-forward + token)."

# -----------------------------------------------------------------------------
section "1. GCP — write to GSM (product-first project)"
# -----------------------------------------------------------------------------

note "Prerequisite: gcloud auth login + project=product-first"
cmd "gcloud auth login"
cmd "gcloud config set project product-first"
echo ""

note "Read the role password auth-ui already wrote to GSM"
cmd 'PWD=$(gcloud secrets versions access latest --secret=cnpg-automated-agent-password --project=product-first)'
echo ""

note "Construct DSN — libpq form (sslmode=require). PR #27 onward translates internally."
cmd 'DSN="postgresql://leartech_automated_agent:${PWD}@leartech-staging-rw.cnpg-system.svc.cluster.local:5432/leartech_automated_agent?sslmode=require"'
echo ""

note "Write a new version (or initial if doesn't exist)"
cmd 'echo -n "$DSN" | gcloud secrets versions add automated-agent-db-dsn --data-file=- --project=product-first'
echo ""

note "Verify ExternalSecret picks it up (should flip to SUCCESS within ~30s)"
cmd "kubectl --context=gke_product-first_us-east1-b_tf-jx-usable-bird -n jx-staging get externalsecret leartech-automated-agent-db -w"
echo ""

# -----------------------------------------------------------------------------
section "2. AZ — write to Vault (modernburro cluster)"
# -----------------------------------------------------------------------------

note "Terminal 1: port-forward to Vault"
cmd "kubectl --context=modern-burro port-forward -n jx-vault svc/vault 8200:8200"
echo ""

note "Terminal 2: set VAULT_TOKEN from cluster unseal secret"
cmd 'export VAULT_TOKEN=$(kubectl --context=modern-burro get secret vault-unseal-keys -n jx-vault -o jsonpath="{.data.vault-root}" | base64 -d)'
echo ""

note "Verify Vault is reachable"
cmd 'curl -sk -H "X-Vault-Token: $VAULT_TOKEN" https://127.0.0.1:8200/v1/sys/health | jq'
echo ""

note "Read the role password auth-ui already wrote to Vault"
cmd 'PWD=$(curl -sk -H "X-Vault-Token: $VAULT_TOKEN" https://127.0.0.1:8200/v1/secret/data/automated-agent/cnpg-automated-agent-password | jq -r .data.data.password)'
echo ""

note "Construct DSN — same form as GCP"
cmd 'DSN="postgresql://leartech_automated_agent:${PWD}@leartech-staging-rw.cnpg-system.svc.cluster.local:5432/leartech_automated_agent?sslmode=require"'
echo ""

note "Write to Vault (path: secret/data/automated-agent/automated-agent-db, property: dsn)"
cmd 'curl -sk -X POST "https://127.0.0.1:8200/v1/secret/data/automated-agent/automated-agent-db" -H "X-Vault-Token: $VAULT_TOKEN" -d "{\"data\": {\"dsn\": \"${DSN}\"}}"'
echo ""

note "Verify ExternalSecret picks it up"
cmd "kubectl --context=modern-burro -n jx-staging get externalsecret leartech-automated-agent-db -w"
echo ""

# -----------------------------------------------------------------------------
section "3. Restart workload to pick up new DSN"
# -----------------------------------------------------------------------------

note "GCP — only needed if rollout is not already in flight from a chart change"
cmd "kubectl --context=gke_product-first_us-east1-b_tf-jx-usable-bird -n jx-staging rollout restart deploy/leartech-automated-agent"
echo ""

note "AZ"
cmd "kubectl --context=modern-burro -n jx-staging rollout restart deploy/leartech-automated-agent"
echo ""

note "Verify pods Ready"
cmd "kubectl --context=gke_product-first_us-east1-b_tf-jx-usable-bird -n jx-staging get pods -l app.kubernetes.io/instance=leartech-automated-agent"
cmd "kubectl --context=modern-burro -n jx-staging get pods -l app.kubernetes.io/instance=leartech-automated-agent"
echo ""

# -----------------------------------------------------------------------------
section "4. End-to-end smoke test"
# -----------------------------------------------------------------------------

note "Port-forward to the GCP service"
cmd "kubectl --context=gke_product-first_us-east1-b_tf-jx-usable-bird -n jx-staging port-forward svc/leartech-automated-agent 8080:8080 &"
echo ""

note "Confirm DB-backed catalog responds (should return JSON list, not 503)"
cmd "curl -s http://localhost:8080/initiatives/catalog | jq 'length'"
echo ""

note "Fire a small initiative and watch the durable run record persist"
cmd 'curl -s -X POST http://localhost:8080/initiatives -H "content-type: application/json" -d "{\"initiative\":\"mortgages-gw-add-changelog-stub\"}" | jq'
echo ""

cmd "curl -s http://localhost:8080/initiatives | jq '.[0]'"
echo ""

echo "=== Done ==="
