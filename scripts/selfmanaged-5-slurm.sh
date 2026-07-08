#!/usr/bin/env bash
#
# selfmanaged-5-slurm.sh - Phase 7 (gap #3): install the Slurm (slinky) stack + its
# prerequisites onto the self-managed workload cluster `caps-self`.
#
# Closes the last "unscripted" gap in the self-managed bring-up: previously the
# slinky install was ad-hoc `helm` commands recorded only in docs/repo notes.
# This makes it reproducible and mentor-portable. Called by
# scripts/selfmanaged-setup.sh (WITH_SLURM=1) or standalone.
#
# Runs ON the CAPZ management VM (pulls the workload kubeconfig from the
# `caps-self-kubeconfig` secret; installs helm to ~/.local/bin if absent, no sudo).
# Drive from a workstation with:
#     bash scripts/azure-remote.sh sync
#     bash scripts/azure-remote.sh ssh 'bash scripts/selfmanaged-5-slurm.sh'
#
# INSTALL ORDER (each idempotent via `helm upgrade --install` / `kubectl apply`):
#   1. cert-manager                    (slurm-operator webhooks depend on it)
#   2. local-path-provisioner          (default StorageClass for slurmctld state)
#   3. namespaces slinky + slurm       (slurm ns => pod-security enforce=privileged
#                                        so the privileged/hostNetwork slurmd runs)
#   4. slurm-operator-crds + slurm-operator (ns slinky)
#   5. slurm cluster (ns slurm, values caps-self-slurm.yaml)
#
# TOPOLOGY DEPENDENCY: caps-self-slurm.yaml pins slurmctld/slurmrestd to a node
# labeled `slinky.slurm.net/node-type=controller` and slurmd to nodes labeled
# `slinky.slurm.net/node-type=compute`. If those labels are absent the head pods
# stay Pending — this script PRE-FLIGHT WARNS (it will not guess which node is
# which). Bring up the controller node first (scripts/selfmanaged-4-controller.sh) and
# label the compute workers, or override VALUES with a topology-appropriate file.
#
# Env overrides:
#   CLUSTER NAMESPACE VALUES
#   CHART_VERSION CERT_MANAGER_VERSION LOCAL_PATH_VERSION
#   SLINKY_NS SLURM_NS SLURM_RELEASE
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CLUSTER="${CLUSTER:-caps-self}"
NAMESPACE="${NAMESPACE:-default}"
VALUES="${VALUES:-$REPO/caps-self-slurm.yaml}"

CHART_VERSION="${CHART_VERSION:-1.1.1}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.18.2}"
LOCAL_PATH_VERSION="${LOCAL_PATH_VERSION:-v0.0.32}"

SLINKY_NS="${SLINKY_NS:-slinky}"
SLURM_NS="${SLURM_NS:-slurm}"
SLURM_RELEASE="${SLURM_RELEASE:-slurm}"

CHART_BASE="oci://ghcr.io/slinkyproject/charts"

[[ -f "$VALUES" ]] || { echo "ERROR: values file not found: $VALUES" >&2; exit 1; }

CTX="$(kubectl config current-context)"
echo ">> mgmt context: $CTX"

# --- workload kubeconfig (pull from the CAPI-managed secret) ---------------------
KC="$(mktemp "/tmp/${CLUSTER}.kubeconfig.XXXXXX")"
trap 'rm -f "$KC"' EXIT
kubectl --context "$CTX" -n "$NAMESPACE" get secret "${CLUSTER}-kubeconfig" \
  -o jsonpath='{.data.value}' | base64 -d > "$KC"
chmod 600 "$KC"
k() { kubectl --kubeconfig "$KC" "$@"; }
echo ">> workload nodes:"
k get nodes -L slinky.slurm.net/node-type || true

# --- helm (install to ~/.local/bin if absent; no sudo) --------------------------
export PATH="$HOME/.local/bin:$PATH"
if ! command -v helm >/dev/null 2>&1; then
  echo ">> helm not found; installing to ~/.local/bin"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    | HELM_INSTALL_DIR="$HOME/.local/bin" USE_SUDO=false bash
fi
h() { helm --kubeconfig "$KC" "$@"; }
echo ">> helm: $(helm version --short)"

# --- pre-flight: topology labels caps-self-slurm.yaml expects -------------------
missing=0
if [[ -z "$(k get nodes -l slinky.slurm.net/node-type=controller -o name 2>/dev/null)" ]]; then
  echo ">> WARN: no node labeled slinky.slurm.net/node-type=controller — slurmctld/"
  echo "         slurmrestd will stay Pending. Bring up the controller node first:"
  echo "           bash scripts/selfmanaged-4-controller.sh"
  echo "         or: kubectl --kubeconfig <wl> label node <n> slinky.slurm.net/node-type=controller"
  missing=1
fi
if [[ -z "$(k get nodes -l slinky.slurm.net/node-type=compute -o name 2>/dev/null)" ]]; then
  echo ">> WARN: no node labeled slinky.slurm.net/node-type=compute — slurmd (DaemonSet)"
  echo "         will have nowhere to run. Label the HPC worker(s):"
  echo "           kubectl --kubeconfig <wl> label node <n> slinky.slurm.net/node-type=compute"
  missing=1
fi
if [[ "$missing" == "1" && "${FORCE:-0}" != "1" ]]; then
  echo ">> refusing to install with the head/compute placement unsatisfiable."
  echo ">> re-run with FORCE=1 to install anyway (pods will Pend until labels exist)."
  exit 1
fi

# --- 1. cert-manager (slurm-operator webhook prerequisite) ----------------------
echo ">> installing cert-manager $CERT_MANAGER_VERSION"
h upgrade --install cert-manager cert-manager \
  --repo https://charts.jetstack.io --namespace cert-manager --create-namespace \
  --version "$CERT_MANAGER_VERSION" --set crds.enabled=true
echo ">> waiting for cert-manager webhook to be ready"
k -n cert-manager rollout status deploy/cert-manager-webhook --timeout=300s || true

# --- 2. local-path-provisioner (default StorageClass) ---------------------------
echo ">> installing local-path-provisioner $LOCAL_PATH_VERSION"
k apply -f "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_VERSION}/deploy/local-path-storage.yaml"
k -n local-path-storage rollout status deploy/local-path-provisioner --timeout=180s || true
echo ">> marking local-path the default StorageClass"
k patch storageclass local-path \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' || true

# --- 3. namespaces (slurm ns needs privileged pod-security for hostNetwork slurmd) ---
for ns in "$SLINKY_NS" "$SLURM_NS"; do
  k create namespace "$ns" --dry-run=client -o yaml | k apply -f -
done
k label namespace "$SLURM_NS" pod-security.kubernetes.io/enforce=privileged --overwrite

# --- 4. slinky operator (CRDs + controller) -------------------------------------
echo ">> installing slurm-operator-crds + slurm-operator (chart $CHART_VERSION, ns $SLINKY_NS)"
h upgrade --install slurm-operator-crds "$CHART_BASE/slurm-operator-crds" \
  --version "$CHART_VERSION" -n "$SLINKY_NS"
h upgrade --install slurm-operator "$CHART_BASE/slurm-operator" \
  --version "$CHART_VERSION" -n "$SLINKY_NS"
echo ">> waiting for the slurm-operator to be ready"
k -n "$SLINKY_NS" rollout status deploy -l app.kubernetes.io/name=slurm-operator --timeout=300s || true

# --- 5. slurm cluster -----------------------------------------------------------
echo ">> installing slurm cluster '$SLURM_RELEASE' (ns $SLURM_NS, values $(basename "$VALUES"))"
h upgrade --install "$SLURM_RELEASE" "$CHART_BASE/slurm" \
  --version "$CHART_VERSION" -n "$SLURM_NS" -f "$VALUES"

# --- verify ---------------------------------------------------------------------
echo ">> slurm pods:"
k -n "$SLURM_NS" get pods -o wide || true
echo
echo ">> done. Check the scheduler once slurmctld+slurmd are Running:"
echo "   kubectl --kubeconfig <workload> -n $SLURM_NS exec deploy/slurm-restapi -- sinfo   # or exec into a slurmd pod"
