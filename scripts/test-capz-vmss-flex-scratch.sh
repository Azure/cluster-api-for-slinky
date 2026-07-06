#!/usr/bin/env bash
#
# test-capz-vmss-flex-scratch.sh - validate the CAPZ VMSS-Flex fork automation on a
# THROWAWAY kind cluster, with ZERO risk to the live mgmt cluster or caps-self.
#
# It reproduces EXACTLY what the Pulumi ClusterAPIOperator emits for the fork
# (pulumi/stacks/control_plane/capi/_operator.py :: _azure_vmss_flex_provider_spec):
# an azure InfrastructureProvider that keeps the upstream v1.24.1 fetch but
#   * overrides the manager container image to the preloaded fork image, and
#   * RFC6902-patches virtualMachineScaleSetID into the two CAPZ CRDs.
# Then it verifies the operator actually installs the fork image and serves the new
# field. This is the integration counterpart to the unit test
# tests/unit/test_control_plane_azure_spec.py::test_azure_vmss_flex_provider_spec_overlay_shape.
#
# ISOLATION: creates its OWN kind cluster (default name capz-fork-test), separate from
# the live mgmt cluster (mgmt-9eb1cd82). It never talks to caps-self. Delete it any time
# with `kind delete cluster --name capz-fork-test` (or run with TEARDOWN=1).
#
# Runs ON the CAPZ mgmt VM (needs docker + kind + kubectl + helm + internet egress; the
# operator fetches the upstream CAPZ v1.24.1 release from GitHub). It does NOT need Azure
# credentials -- the CAPZ controller boots and serves CRDs without an AzureClusterIdentity.
#
# Env overrides:
#   TEST_CLUSTER (default capz-fork-test)
#   IMG          (default local/capz-vmss-flex-amd64:v1.24.1-vmss - MUST match the Pulumi
#                 azure provider imageUrl / build-capz-vmss-flex.sh IMG)
#   CAPI_VERSION (default v1.13.2)      CAPZ_VERSION (default v1.24.1)
#   OPERATOR_CHART_VERSION (default 0.27.0)   CERT_MANAGER_VERSION (default v1.20.2)
#   GITHUB_TOKEN (optional; avoids GitHub API rate limits during the provider fetch)
#   TEARDOWN=1   (delete the test cluster and exit; skips everything else)
set -euo pipefail

# On the mgmt VM helm is installed under ~/.local/bin (day2-selfmanaged.sh), which is
# not on the non-interactive ssh PATH. Make sure user-local tools are found.
export PATH="$HOME/.local/bin:$PATH"

TEST_CLUSTER="${TEST_CLUSTER:-capz-fork-test}"
IMG="${IMG:-local/capz-vmss-flex-amd64:v1.24.1-vmss}"
CAPI_VERSION="${CAPI_VERSION:-v1.13.2}"
CAPZ_VERSION="${CAPZ_VERSION:-v1.24.1}"
OPERATOR_CHART_VERSION="${OPERATOR_CHART_VERSION:-0.27.0}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.20.2}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUBECONFIG_FILE="$(mktemp /tmp/capz-fork-test-kubeconfig-XXXXXX)"
export KUBECONFIG="$KUBECONFIG_FILE"
AM_CRD="azuremachines.infrastructure.cluster.x-k8s.io"
AMT_CRD="azuremachinetemplates.infrastructure.cluster.x-k8s.io"

log()  { printf '\n>> %s\n' "$*"; }
fail() { printf '   FAIL: %s\n' "$*"; }
pass() { printf '   PASS: %s\n' "$*"; }

if [[ "${TEARDOWN:-0}" == "1" ]]; then
  log "tearing down test cluster $TEST_CLUSTER"
  kind delete cluster --name "$TEST_CLUSTER" || true
  rm -f "$KUBECONFIG_FILE"
  exit 0
fi

# --- 1. throwaway kind cluster (separate from the live mgmt cluster) -------------
if kind get clusters | grep -qx "$TEST_CLUSTER"; then
  log "reusing existing test cluster $TEST_CLUSTER"
else
  log "creating throwaway kind cluster $TEST_CLUSTER"
  kind create cluster --name "$TEST_CLUSTER"
fi
kind get kubeconfig --name "$TEST_CLUSTER" > "$KUBECONFIG_FILE"

# --- 2. build + preload the fork image INTO the test cluster --------------------
# Reuses the build/preload script, retargeted at the test cluster. Idempotent
# (docker build is layer-cached on re-runs).
log "building + preloading $IMG into $TEST_CLUSTER"
KIND_CLUSTER="$TEST_CLUSTER" IMG="$IMG" BASE_TAG="$CAPZ_VERSION" \
  bash "$SCRIPT_DIR/build-capz-vmss-flex.sh"

# --- 3. cert-manager (operator prerequisite) ------------------------------------
log "installing cert-manager $CERT_MANAGER_VERSION"
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
helm repo update jetstack >/dev/null
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version "$CERT_MANAGER_VERSION" --set crds.enabled=true \
  --wait --timeout 180s

# --- 4. CAPI Operator -----------------------------------------------------------
log "installing cluster-api-operator $OPERATOR_CHART_VERSION"
helm repo add capi-operator \
  https://kubernetes-sigs.github.io/cluster-api-operator >/dev/null 2>&1 || true
helm repo update capi-operator >/dev/null
helm upgrade --install capi-operator capi-operator/cluster-api-operator \
  --namespace capi-operator-system --create-namespace \
  --version "$OPERATOR_CHART_VERSION" --wait --timeout 180s

# Optional GitHub token secret to dodge API rate limits during the release fetch.
kubectl create namespace capz-system --dry-run=client -o yaml | kubectl apply -f -
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  kubectl -n capz-system create secret generic azure-variables \
    --from-literal=github-token="$GITHUB_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

# --- 5. provider CRs: CoreProvider + the azure InfrastructureProvider (fork) -----
# The InfrastructureProvider spec below is byte-for-byte what the Pulumi component
# emits when azure_vmss_flex_image is set (version stays upstream v1.24.1; only the
# manager image + the two CRD schemas change).
log "applying CoreProvider + fork azure InfrastructureProvider"
kubectl create namespace capi-system --dry-run=client -o yaml | kubectl apply -f -
config_secret_snippet=""
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  config_secret_snippet=$'  configSecret:\n    name: azure-variables'
fi
kubectl apply -f - <<EOF
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: CoreProvider
metadata:
  name: cluster-api
  namespace: capi-system
spec:
  version: ${CAPI_VERSION}
  manager:
    featureGates:
      ClusterTopology: true
---
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: InfrastructureProvider
metadata:
  name: azure
  namespace: capz-system
spec:
  version: ${CAPZ_VERSION}
${config_secret_snippet}
  manager:
    featureGates:
      ClusterTopology: true
  deployment:
    containers:
      - name: manager
        imageUrl: ${IMG}
  patches:
    - target:
        kind: CustomResourceDefinition
        name: ${AM_CRD}
      patch: |
        [{"op":"add","path":"/spec/versions/0/schema/openAPIV3Schema/properties/spec/properties/virtualMachineScaleSetID","value":{"type":"string","description":"VMSS Flex resource id (backport)"}}]
    - target:
        kind: CustomResourceDefinition
        name: ${AMT_CRD}
      patch: |
        [{"op":"add","path":"/spec/versions/0/schema/openAPIV3Schema/properties/spec/properties/template/properties/spec/properties/virtualMachineScaleSetID","value":{"type":"string","description":"VMSS Flex resource id (backport)"}}]
EOF

log "waiting for the azure InfrastructureProvider to reconcile (up to 5m)"
kubectl wait --for=condition=Ready infrastructureprovider/azure \
  -n capz-system --timeout=300s || \
  log "provider not Ready yet -- continuing to concrete checks (CRDs apply even if the controller is unhealthy)"

# --- 6. verify ------------------------------------------------------------------
log "VERIFY"
failures=0

# (a) controller image is the fork
img="$(kubectl -n capz-system get deploy capz-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].image}' 2>/dev/null || true)"
if [[ "$img" == "$IMG" ]]; then pass "manager image == $IMG"; else fail "manager image is '$img' (expected $IMG)"; failures=$((failures + 1)); fi

# (b) the field is served on both CRDs
am_type="$(kubectl get crd "$AM_CRD" -o jsonpath='{.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties.virtualMachineScaleSetID.type}' 2>/dev/null || true)"
if [[ "$am_type" == "string" ]]; then pass "$AM_CRD serves virtualMachineScaleSetID"; else fail "$AM_CRD missing the field (type='$am_type')"; failures=$((failures + 1)); fi
amt_type="$(kubectl get crd "$AMT_CRD" -o jsonpath='{.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties.template.properties.spec.properties.virtualMachineScaleSetID.type}' 2>/dev/null || true)"
if [[ "$amt_type" == "string" ]]; then pass "$AMT_CRD serves virtualMachineScaleSetID"; else fail "$AMT_CRD missing the field (type='$amt_type')"; failures=$((failures + 1)); fi

# (c) controller pod actually Running on the preloaded image (catches ImagePullBackOff
#     => would mean the rendered Deployment forced imagePullPolicy: Always and we need
#     a 3rd patch)
phase="$(kubectl -n capz-system get pods -l control-plane=capz-controller-manager \
  -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)"
if [[ "$phase" == "Running" ]]; then pass "capz-controller-manager pod Running (preloaded image pulled)"; else
  fail "capz-controller-manager pod phase='$phase' (check for ImagePullBackOff -> imagePullPolicy)"; failures=$((failures + 1)); fi

echo
kubectl -n capz-system get pods 2>/dev/null | head || true
echo
if [[ "$failures" -eq 0 ]]; then
  log "RESULT: PASS -- the operator installs the fork image and serves virtualMachineScaleSetID."
  echo "   Tear down when done:  kind delete cluster --name $TEST_CLUSTER"
  exit 0
else
  log "RESULT: FAIL -- $failures check(s) failed. Inspect with:"
  echo "   KUBECONFIG=$KUBECONFIG_FILE kubectl -n capz-system describe infrastructureprovider azure"
  echo "   KUBECONFIG=$KUBECONFIG_FILE kubectl -n capz-system get pods"
  exit 1
fi
