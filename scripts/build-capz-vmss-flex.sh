#!/usr/bin/env bash
#
# build-capz-vmss-flex.sh - BUILD + PRELOAD ONLY: produce the backported CAPZ
# VMSS-Flex controller image and load it into the kind management cluster.
#
# This is the BUILD half of the old scripts/deploy-capz-vmss-flex.sh, split out so
# the INSTALL half is done declaratively by Pulumi instead of by hand. The azure
# InfrastructureProvider CR (see pulumi/stacks/control_plane/capi/_operator.py):
#   * keeps spec.version = v1.24.1 (normal upstream fetch, no fetchConfig),
#   * overrides spec.deployment.containers[].imageUrl to the image built here, and
#   * adds spec.patches that inject virtualMachineScaleSetID into the two CAPZ CRDs.
# The CAPI Operator then reconciles the fork FOR us -- no scaling the operator to 0,
# no hand-editing CRDs, no `kubectl set image`, no drift.
#
# Context: caps-self cross-node InfiniBand RDMA needs both workers in ONE IB
# partition. The CAPZ-managed Uniform AzureMachinePool can't express that. The fork
#   github.com/arsdragonfly/cluster-api-provider-azure @ arsdragonfly/md-vmss (1a6f953)
# adds AzureMachine(.Template).spec.virtualMachineScaleSetID so CAPZ can place worker
# VMs into a PRE-EXISTING (BYO) VMSS *Flexible* configured for IB co-location.
#
# What this script does, and ONLY this:
#   1. cherry-pick the fork's single additive commit onto the CAPZ v1.24.1 tag
#      (matches the Pulumi CAPZ_PROVIDER_VERSION pin -> no control-plane version jump).
#   2. docker build the controller image (the field is compiled into the Go types).
#   3. docker save | ctr import the image into EVERY kind node (no registry needed).
#   4. stop. Installation is Pulumi's job.
#
# Runs ON the CAPZ mgmt VM (needs docker + kind + the kind mgmt cluster). It does NOT
# touch the cluster's CAPZ install, CRDs, or workers -- purely build + preload.
#
# IMPORTANT: keep IMG in sync with the imageUrl the Pulumi azure provider sets.
#
# Env overrides:
#   FORK_URL     (default https://github.com/arsdragonfly/cluster-api-provider-azure.git)
#   FORK_BRANCH  (default arsdragonfly/md-vmss)
#   COMMIT       (default 1a6f9535dee96f659b12e89caaddc6d9fdf493ec)
#   BASE_TAG     (default v1.24.1 - must match the Pulumi CAPZ pin)
#   WORKDIR      (default ~/capz-vmss-flex)
#   IMG          (default local/capz-vmss-flex-amd64:v1.24.1-vmss)
#   KIND_CLUSTER (default mgmt-9eb1cd82)
#   ARCH         (default amd64)
set -euo pipefail

FORK_URL="${FORK_URL:-https://github.com/arsdragonfly/cluster-api-provider-azure.git}"
FORK_BRANCH="${FORK_BRANCH:-arsdragonfly/md-vmss}"
COMMIT="${COMMIT:-1a6f9535dee96f659b12e89caaddc6d9fdf493ec}"
BASE_TAG="${BASE_TAG:-v1.24.1}"
WORKDIR="${WORKDIR:-$HOME/capz-vmss-flex}"
IMG="${IMG:-local/capz-vmss-flex-amd64:v1.24.1-vmss}"
KIND_CLUSTER="${KIND_CLUSTER:-mgmt-9eb1cd82}"
ARCH="${ARCH:-amd64}"
BUILD_BRANCH="vmss-flex-${BASE_TAG}"
UPSTREAM="https://github.com/kubernetes-sigs/cluster-api-provider-azure.git"

log() { printf '\n>> %s\n' "$*"; }

# --- 1. source: cherry-pick the additive commit onto BASE_TAG -------------------
log "preparing backport source in $WORKDIR (base $BASE_TAG + ${COMMIT:0:7})"
if [[ ! -d "$WORKDIR/.git" ]]; then
  git clone --filter=blob:none -q "$FORK_URL" "$WORKDIR"
fi
cd "$WORKDIR"
git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM"
git fetch -q upstream --tags
git fetch -q origin "$FORK_BRANCH"
if ! git rev-parse --verify "$BUILD_BRANCH" >/dev/null 2>&1; then
  git checkout -q -B "$BUILD_BRANCH" "$BASE_TAG"
  # The commit is purely additive (+323/-1); it applies cleanly onto v1.24.1. If a
  # future base tag conflicts, resolve by hand then re-run.
  git cherry-pick -x "$COMMIT"
else
  git checkout -q "$BUILD_BRANCH"
fi
grep -q "VirtualMachineScaleSetID" api/v1beta1/azuremachine_types.go \
  || { echo "ERROR: backport not present in source tree (cherry-pick failed?)" >&2; exit 1; }
log "backport HEAD: $(git rev-parse --short HEAD) on $BUILD_BRANCH"

# --- 2. build the controller image (raw docker build = no extra Make tooling) ---
# --provenance=false --sbom=false: Docker 29 BuildKit otherwise emits an OCI image
# INDEX with attestation manifests that the containerd import in step 3 cannot load.
log "building controller image $IMG (this takes a few minutes)"
DOCKER_BUILDKIT=1 docker build --provenance=false --sbom=false \
  --build-arg ARCH="$ARCH" -t "$IMG" .

# --- 3. preload into every kind node (no registry required) ---------------------
# NB: `kind load docker-image` fails on Docker 29 ("failed to detect containerd
# snapshotter") because of the containerd image store. Bypass it by saving the image
# and importing straight into each kind node's containerd (k8s.io namespace). All
# nodes are loaded because PKO can schedule the CAPZ controller Deployment anywhere.
log "preloading $IMG into kind cluster $KIND_CLUSTER nodes (ctr import)"
TMPTAR="$(mktemp /tmp/capz-vmss-flex-XXXXXX.tar)"
trap 'rm -f "$TMPTAR"' EXIT
docker save "$IMG" -o "$TMPTAR"
for node in $(kind get nodes --name "$KIND_CLUSTER"); do
  echo "  -> $node"
  docker exec -i "$node" ctr --namespace=k8s.io images import - < "$TMPTAR"
done

log "DONE. Image $IMG built and preloaded on all $KIND_CLUSTER nodes."
cat <<EOF

Next (declarative install -- handled by Pulumi, NOT this script):
  The azure InfrastructureProvider CR keeps spec.version=v1.24.1 (upstream fetch) and adds:
    spec.deployment.containers[].imageUrl = $IMG
    spec.patches -> RFC6902 'add' of virtualMachineScaleSetID at /spec/versions/0/... on
      the azuremachines + azuremachinetemplates CRDs.
  Enable the fork toggle and run 'pulumi up'; the CAPI Operator reconciles the fork
  controller + patched CRDs. Because the tag is non-'latest', kubelet defaults to
  imagePullPolicy=IfNotPresent and uses this preloaded image (no registry pull).
EOF
