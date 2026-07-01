#!/usr/bin/env bash
#
# deploy-capz-vmss-flex.sh - EXPERIMENTAL: deploy a backport of the mentor's CAPZ
# VMSS-Flex attachment field onto the running CAPZ v1.23.2 (Option A).
#
# Context: caps-self cross-node InfiniBand RDMA needs both workers in ONE IB
# partition. The CAPZ-managed Uniform AzureMachinePool can't express that
# (singlePlacementGroup/PPG not exposed). The fork
#   github.com/arsdragonfly/cluster-api-provider-azure @ arsdragonfly/md-vmss (1a6f953)
# adds AzureMachine(.Template).spec.virtualMachineScaleSetID so CAPZ can place
# worker VMs into a PRE-EXISTING (BYO) VMSS *Flexible* that you configure for IB
# co-location (single fault domain + proximity placement group).
#
# The fork is built on CAPZ main (~v1.24, CAPI v1.13); this mgmt cluster runs CAPZ
# v1.23.2 / CAPI v1.12.8. To avoid a control-plane version jump for an experiment,
# this CHERRY-PICKS the single additive commit onto the v1.23.2 tag (it applies
# cleanly: v1.23.2 already has armcompute/v5 + capacityReservationGroupID), builds
# the controller image, loads it into the kind mgmt cluster (no registry needed),
# surgically adds the new field to the two live CRDs (preserving their conversion
# webhook), and swaps the controller image.
#
# Runs ON the CAPZ mgmt VM (kind cluster). Reversible (see REVERT below). It does
# NOT touch the workers — that destructive cutover is a separate step.
#
# Env overrides:
#   FORK_URL    (default github.com/arsdragonfly/cluster-api-provider-azure)
#   FORK_BRANCH (default arsdragonfly/md-vmss)
#   COMMIT      (default 1a6f9535dee96f659b12e89caaddc6d9fdf493ec)
#   BASE_TAG    (default v1.23.2 - must match the running CAPZ)
#   WORKDIR     (default ~/capz-vmss-flex)
#   IMG         (default local/capz-vmss-flex-amd64:v1.23.2-vmss)
#   KIND_CLUSTER(default mgmt-9eb1cd82)
#
# NOTE: this mgmt cluster's CAPZ is managed by the cluster-api-operator, which
# reconciles the azure InfrastructureProvider back to upstream v1.23.2 (reverting
# BOTH the controller image AND the CRDs). This script scales that operator to 0
# so the experimental patch sticks.
#
# REVERT (restores upstream CAPZ v1.23.2, drops the field):
#   kubectl -n capi-operator-system scale deploy/cluster-api-operator --replicas=1
#   # the operator then re-reconciles azure back to v1.23.2; or do it explicitly:
#   kubectl -n capz-system set image deploy/capz-controller-manager \
#     manager=registry.k8s.io/cluster-api-azure/cluster-api-azure-controller:v1.23.2
set -euo pipefail

FORK_URL="${FORK_URL:-https://github.com/arsdragonfly/cluster-api-provider-azure.git}"
FORK_BRANCH="${FORK_BRANCH:-arsdragonfly/md-vmss}"
COMMIT="${COMMIT:-1a6f9535dee96f659b12e89caaddc6d9fdf493ec}"
BASE_TAG="${BASE_TAG:-v1.23.2}"
WORKDIR="${WORKDIR:-$HOME/capz-vmss-flex}"
IMG="${IMG:-local/capz-vmss-flex-amd64:v1.23.2-vmss}"
KIND_CLUSTER="${KIND_CLUSTER:-mgmt-9eb1cd82}"
BUILD_BRANCH="vmss-flex-${BASE_TAG}"
UPSTREAM="https://github.com/kubernetes-sigs/cluster-api-provider-azure.git"

AM_CRD="azuremachines.infrastructure.cluster.x-k8s.io"
AMT_CRD="azuremachinetemplates.infrastructure.cluster.x-k8s.io"
FIELD_DESC="VMSS Flex resource id (backport of ${FORK_BRANCH} ${COMMIT:0:7})"

log() { printf '\n>> %s\n' "$*"; }

# --- 1. source: cherry-pick the additive commit onto BASE_TAG -------------------
log "preparing backport source in $WORKDIR (base $BASE_TAG + $COMMIT)"
if [[ ! -d "$WORKDIR/.git" ]]; then
  git clone --filter=blob:none -q "$FORK_URL" "$WORKDIR"
fi
cd "$WORKDIR"
git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM"
git fetch -q upstream --tags
git fetch -q origin "$FORK_BRANCH"
if ! git rev-parse --verify "$BUILD_BRANCH" >/dev/null 2>&1; then
  git checkout -q -B "$BUILD_BRANCH" "$BASE_TAG"
  git cherry-pick -x "$COMMIT"
else
  git checkout -q "$BUILD_BRANCH"
fi
grep -q "VirtualMachineScaleSetID" api/v1beta1/azuremachine_types.go \
  || { echo "ERROR: backport not present in source tree" >&2; exit 1; }
log "backport HEAD: $(git rev-parse --short HEAD) on $BUILD_BRANCH"

# --- 2. build the controller image (raw docker build = no extra Make tooling) ---
# --provenance=false --sbom=false: Docker 29 BuildKit otherwise emits an OCI image
# INDEX with attestation manifests, which `kind load docker-image` cannot import
# ("failed to detect containerd snapshotter"). These flags force a single,
# loadable image.
log "building controller image $IMG (this takes a few minutes)"
DOCKER_BUILDKIT=1 docker build --provenance=false --sbom=false --build-arg ARCH=amd64 -t "$IMG" .

# --- 3. load into the kind mgmt cluster (no registry required) ------------------
# NB: `kind load docker-image` fails on Docker 29 ("failed to detect containerd
# snapshotter") because of the containerd image store. Bypass it by saving the
# image and importing straight into each kind node's containerd (k8s.io namespace).
log "loading $IMG into kind cluster $KIND_CLUSTER nodes (ctr import)"
TMPTAR="$(mktemp /tmp/capz-vmss-flex-XXXXXX.tar)"
trap 'rm -f "$TMPTAR"' EXIT
docker save "$IMG" -o "$TMPTAR"
for node in $(kind get nodes --name "$KIND_CLUSTER"); do
  echo "  -> $node"
  docker exec -i "$node" ctr --namespace=k8s.io images import - < "$TMPTAR"
done

# --- 3b. pause the CAPI Operator so it doesn't revert our image/CRD patch --------
# The cluster-api-operator reconciles the azure InfrastructureProvider back to
# upstream v1.23.2 (clobbers both the controller image and the CRDs). Scale it to
# 0 for the duration of the experiment. REVERT: scale back to 1 (see header).
OPERATOR_NS="${OPERATOR_NS:-capi-operator-system}"
OPERATOR_DEPLOY="${OPERATOR_DEPLOY:-cluster-api-operator}"
if kubectl -n "$OPERATOR_NS" get deploy "$OPERATOR_DEPLOY" >/dev/null 2>&1; then
  log "pausing CAPI Operator ($OPERATOR_NS/$OPERATOR_DEPLOY) so it won't revert the patch"
  kubectl -n "$OPERATOR_NS" scale deploy "$OPERATOR_DEPLOY" --replicas=0
  kubectl -n "$OPERATOR_NS" rollout status deploy "$OPERATOR_DEPLOY" --timeout=60s 2>/dev/null || true
fi

# --- 4. add the new field to the live CRDs (preserve the conversion webhook) -----
# Surgical: kubectl replace from the live object so .spec.conversion (Webhook +
# caBundle) is untouched; a raw apply of the regenerated base CRD would strip it.
inject_field() {
  local crd="$1" path="$2" present
  present="$(kubectl get crd "$crd" -o json | jq -r "$path.type // \"no\"")"
  if [[ "$present" == "string" ]]; then
    log "CRD $crd already has virtualMachineScaleSetID — skipping"
    return 0
  fi
  log "injecting virtualMachineScaleSetID into CRD $crd"
  kubectl get crd "$crd" -o json \
    | jq "$path = {type:\"string\", description:\"$FIELD_DESC\"}" \
    | kubectl replace -f -
}
# AzureMachine: ...properties.spec.properties.virtualMachineScaleSetID
inject_field "$AM_CRD" \
  '(.spec.versions[]|select(.name=="v1beta1").schema.openAPIV3Schema.properties.spec.properties.virtualMachineScaleSetID)'
# AzureMachineTemplate: ...properties.spec.properties.template.properties.spec.properties.virtualMachineScaleSetID
inject_field "$AMT_CRD" \
  '(.spec.versions[]|select(.name=="v1beta1").schema.openAPIV3Schema.properties.spec.properties.template.properties.spec.properties.virtualMachineScaleSetID)'

# --- 5. swap the controller image (kept in-cluster; no pull) ---------------------
log "pointing capz-controller-manager at $IMG"
kubectl -n capz-system set image deployment/capz-controller-manager manager="$IMG"
kubectl -n capz-system patch deployment capz-controller-manager \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"manager","imagePullPolicy":"IfNotPresent"}]}}}}'
kubectl -n capz-system rollout status deployment/capz-controller-manager --timeout=180s

# --- 6. verify the field is live -------------------------------------------------
log "verifying the field is served"
kubectl explain azuremachinetemplate.spec.template.spec.virtualMachineScaleSetID 2>/dev/null \
  | sed -n '1,4p' || true
log "DONE. CAPZ now accepts virtualMachineScaleSetID. Workers are unchanged."
