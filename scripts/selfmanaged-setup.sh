#!/usr/bin/env bash
#
# selfmanaged-setup.sh - one entry point that spins up the self-managed workload
# cluster (`caps-self`) from scratch, in the correct order, idempotently.
#
# This is the top-level ORCHESTRATOR that closes the "no single reproducible path"
# gap: previously the phases (BYO networking, CAPZ fork, Flex VMSS, manifest apply,
# Day-2 addons, controller split, Slurm) were separate scripts + manual `az`/`helm`
# steps whose ordering lived only in the operator's head + repo notes. This chains
# the scripted phases with pre-flight checks and readiness waits so a mentor can
# reproduce the cluster with one command (and reconcile it into Pulumi later).
#
# Runs ON the CAPZ management VM (needs the mgmt kind cluster + az UAMI + docker/
# kind for the flex fork). Drive from a workstation with:
#     bash scripts/azure-remote.sh sync
#     bash scripts/azure-remote.sh ssh 'bash scripts/selfmanaged-setup.sh'
#
# ── WORKER MODES ───────────────────────────────────────────────────────────────
#   WORKER_MODE=uniform (DEFAULT): the clean, non-experimental self-managed cluster
#     — BYO networking -> apply manifest (MachinePool workers, Uniform VMSS) ->
#     wait -> Day-2. Cross-node MPI works over TCP. This is the reproducible CORE
#     and the recommended path for a from-scratch reproducibility test (point it at
#     a cheaper CPU SKU via an overlay if you don't need GPUs to prove the flow).
#   WORKER_MODE=flex: adds the InfiniBand-RDMA path on top — deploy the backported
#     CAPZ `virtualMachineScaleSetID` fork, pre-create the BYO Flexible VMSS (single
#     IB partition), and apply the MachineDeployment workers from the .flex.yaml
#     instead of the MachinePool. EXPERIMENTAL (backported controller + operator
#     paused); matches the current live topology.
#
# ── OPT-IN LAYERS ──────────────────────────────────────────────────────────────
#   WITH_CONTROLLER=1  also bring up the dedicated controller node
#                      (caps-self-md-ctrl) via selfmanaged-4-controller.sh (PHASE 1,
#                      additive). Set DO_REPIN=1 to also repin Slurm + taint compute
#                      (disruptive; passed through to selfmanaged-4-controller.sh).
#   WITH_SLURM=1       install the Slurm (slinky) stack via
#                      scripts/selfmanaged-5-slurm.sh (cert-manager + local-path +
#                      slinky operator + slurm cluster).
#
# ── RUN CONTROL ────────────────────────────────────────────────────────────────
#   PLAN=1             dry-run: print the ordered stage list + commands, run nothing.
#   STAGES="a b c"     run only these stages (space/comma separated), skipping the
#                      auto-derived list. Stage names: networking fork flexvmss
#                      cluster workers dnslink wait addons controller slurm.
#   Args after `--` are treated the same as STAGES (e.g. `... -- networking cluster`).
#
# Idempotent: every stage no-ops if already done, so re-run after a transient
# failure. NON-destructive by default; the flex fork pauses the CAPI operator and
# DO_REPIN restarts Slurm — both clearly gated.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$REPO/scripts"

CLUSTER="${CLUSTER:-caps-self}"
NAMESPACE="${NAMESPACE:-default}"
BASE_MANIFEST="${BASE_MANIFEST:-$REPO/selfmanaged-workload-cluster.yaml}"
FLEX_MANIFEST="${FLEX_MANIFEST:-$REPO/selfmanaged-workload-cluster.flex.yaml}"

# Workload RG (where CAPZ creates the <cluster>.capz.io private DNS zone) + the
# management VNet to link that zone to (so the mgmt cluster resolves the workload
# apiserver FQDN). Defaults match the networking script / live caps-self.
RG="${RG:-rg-capz-mi-scus}"
MGMT_RG="${MGMT_RG:-rg-capz-mi-dev2}"
MGMT_VNET="${MGMT_VNET:-vm-capz-mi-devVNET}"

WORKER_MODE="${WORKER_MODE:-uniform}"
WITH_CONTROLLER="${WITH_CONTROLLER:-0}"
WITH_SLURM="${WITH_SLURM:-0}"
DO_REPIN="${DO_REPIN:-0}"
PLAN="${PLAN:-0}"
CP_WAIT_TIMEOUT="${CP_WAIT_TIMEOUT:-1800}"   # seconds to wait for the control plane

CTX="${CTX:-}"                                # mgmt kube-context (auto-detected)

# ── helpers ─────────────────────────────────────────────────────────────────────
log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

run() {  # echo + execute, or just echo under PLAN=1
  if [[ "$PLAN" == "1" ]]; then printf '   [plan] %s\n' "$*"; return 0; fi
  "$@"
}

kctx() { kubectl --context "$CTX" "$@"; }

# ── pre-flight ──────────────────────────────────────────────────────────────────
preflight() {
  log "pre-flight"
  # Under PLAN=1, live-environment problems are warnings (so you can preview the
  # stage plan off the mgmt VM); otherwise they are fatal.
  local soft=die
  [[ "$PLAN" == "1" ]] && soft=info

  command -v kubectl >/dev/null || "$soft" "kubectl not found (run on the mgmt VM)"
  command -v az      >/dev/null || "$soft" "az CLI not found (run on the mgmt VM)"
  [[ -f "$BASE_MANIFEST" ]] || die "manifest not found: $BASE_MANIFEST"
  [[ "$PLAN" == "1" ]] || az account show >/dev/null 2>&1 || run az login --identity -o none

  [[ -n "$CTX" ]] || CTX="$(kubectl config current-context 2>/dev/null || true)"
  [[ -n "$CTX" ]] || "$soft" "no kube-context; is the mgmt kind cluster up?"
  info "kube-context: ${CTX:-<none>}"
  info "worker mode : $WORKER_MODE  (controller=$WITH_CONTROLLER slurm=$WITH_SLURM)"

  # AzureCluster.identityRef requires this to exist on the mgmt cluster already
  # (created by the Pulumi azure control-plane stack / Phase 1).
  if [[ "$PLAN" != "1" ]] && ! kctx get azureclusteridentity cluster-identity -n "$NAMESPACE" >/dev/null 2>&1; then
    die "AzureClusterIdentity 'cluster-identity' missing in ns/$NAMESPACE — bring up the CAPZ control plane first (pulumi up -s azure)."
  fi

  if [[ "$WORKER_MODE" == "flex" ]]; then
    command -v docker >/dev/null || "$soft" "flex mode needs docker on the mgmt VM (CAPZ fork build)"
    command -v kind   >/dev/null || "$soft" "flex mode needs kind on the mgmt VM"
    [[ -f "$FLEX_MANIFEST" ]] || die "flex manifest not found: $FLEX_MANIFEST"
  fi
}

# ── stages ──────────────────────────────────────────────────────────────────────
stage_networking() {
  log "networking — BYO VNet/subnets/NSGs/NAT gateways/peering"
  run bash "$SCRIPTS/selfmanaged-1-networking.sh"
}

stage_fork() {          # flex only
  log "CAPZ VMSS-Flex fork — controller image + CRD field (experimental)"
  info "pauses the CAPI operator so the backported controller sticks (see script REVERT)"
  run bash "$SCRIPTS/deploy-capz-vmss-flex.sh"
}

stage_flexvmss() {      # flex only
  log "BYO Flexible VMSS — single IB partition for cross-node RDMA"
  run bash "$SCRIPTS/selfmanaged-2-flexvmss.sh"
}

# Apply the base manifest but SKIP the MachinePool worker docs (caps-self-mp-0);
# the flex MachineDeployment workers replace them. Splitting the multi-doc file
# avoids creating a throwaway Uniform VMSS of expensive GPU VMs in flex mode.
apply_cluster_no_workers() {
  local src="$1" tmp
  tmp="$(mktemp -d)"
  awk -v dir="$tmp" '
    BEGIN { n=0; f=sprintf("%s/doc-%03d.yaml", dir, n) }
    /^---[[:space:]]*$/ { n++; f=sprintf("%s/doc-%03d.yaml", dir, n); next }
    { print > f }' "$src"
  local doc applied=0
  for doc in "$tmp"/doc-*.yaml; do
    [[ -s "$doc" ]] || continue
    grep -qE '^kind:[[:space:]]' "$doc" || continue   # skip comment-only docs
    if grep -qE '^kind: (MachinePool|AzureMachinePool)$' "$doc" \
       || grep -qE "^  name: ${CLUSTER}-mp-0$" "$doc"; then
      info "skipping worker doc $(basename "$doc") (replaced by flex MachineDeployment)"
      continue
    fi
    run kctx apply -f "$doc"
    applied=$((applied + 1))
  done
  rm -rf "$tmp"
  info "applied $applied non-worker docs from $(basename "$src")"
}

stage_cluster() {
  log "apply cluster manifest — Cluster + AzureCluster + KubeadmControlPlane"
  if [[ "$WORKER_MODE" == "flex" ]]; then
    apply_cluster_no_workers "$BASE_MANIFEST"
  else
    run kctx apply -f "$BASE_MANIFEST"
  fi
}

stage_workers() {       # flex only — MachineDeployment workers into the Flex VMSS
  log "apply flex workers — MachineDeployment from $(basename "$FLEX_MANIFEST")"
  info "(flex MachineDeployment/AzureMachineTemplate(virtualMachineScaleSetID)/KubeadmConfigTemplate)"
  run kctx apply -f "$FLEX_MANIFEST"
}

# CAPZ auto-creates the private DNS zone <cluster>.capz.io and links it to the
# WORKLOAD VNet, but NOT the mgmt VNet — so the mgmt cluster/CAPI can't resolve
# apiserver.<cluster>.capz.io and the control plane never reports Initialized.
# Link the zone to the mgmt VNet (idempotent). Must run AFTER the cluster apply
# (CAPZ creates the zone during internal-LB provisioning) and BEFORE the wait.
stage_dnslink() {
  log "private-DNS link — let the mgmt VNet resolve apiserver.$CLUSTER.capz.io"
  if [[ "$PLAN" == "1" ]]; then
    info "[plan] wait for zone $CLUSTER.capz.io in $RG, then link vnet -> $MGMT_VNET"
    return 0
  fi
  local zone="${CLUSTER}.capz.io" _
  info "waiting for CAPZ to create private DNS zone $zone in $RG..."
  for _ in $(seq 1 60); do
    az network private-dns zone show -g "$RG" -n "$zone" >/dev/null 2>&1 && break
    sleep 10
  done
  if ! az network private-dns zone show -g "$RG" -n "$zone" >/dev/null 2>&1; then
    info "WARN: zone $zone not present yet — re-run after the LB provisions"; return 0
  fi
  local mgmt_vnet_id
  mgmt_vnet_id="$(az network vnet show -g "$MGMT_RG" -n "$MGMT_VNET" --query id -o tsv 2>/dev/null || true)"
  if [[ -z "$mgmt_vnet_id" ]]; then info "WARN: mgmt VNet $MGMT_VNET not found; skipping"; return 0; fi
  if az network private-dns link vnet show -g "$RG" -z "$zone" -n mgmt-link >/dev/null 2>&1; then
    info "DNS link mgmt-link already exists"
  else
    run az network private-dns link vnet create -g "$RG" -z "$zone" -n mgmt-link \
      --virtual-network "$mgmt_vnet_id" --registration-enabled false -o none
  fi
}

stage_wait() {
  log "wait for the control plane to initialize (up to ${CP_WAIT_TIMEOUT}s)"
  if [[ "$PLAN" == "1" ]]; then info "[plan] wait KubeadmControlPlane Initialized + ${CLUSTER}-kubeconfig secret"; return 0; fi
  # Gate on INITIALIZED (apiserver up + reachable from mgmt), NOT node-Ready: the
  # CP node only goes Ready once Day-2 installs the CNI, and Day-2 runs AFTER this
  # stage. Waiting on the Cluster's ControlPlaneReady here would ALWAYS time out
  # until Day-2 runs. Initialized flips true once kubeadm init is done + the mgmt
  # cluster can resolve/reach the apiserver (i.e. after the dnslink stage).
  kctx wait --for=condition=Initialized kubeadmcontrolplane \
    -l cluster.x-k8s.io/cluster-name="$CLUSTER" -n "$NAMESPACE" \
    --timeout="${CP_WAIT_TIMEOUT}s" || info "WARN: control plane not Initialized yet — Day-2 may need a re-run"
  local _
  for _ in $(seq 1 60); do
    kctx get secret "${CLUSTER}-kubeconfig" -n "$NAMESPACE" >/dev/null 2>&1 && break
    sleep 5
  done
  kctx get "cluster/$CLUSTER" -n "$NAMESPACE" -o wide || true
}

stage_addons() {
  log "addons — cloud-provider-azure (CCM) + Calico CNI (VXLAN) + kube-proxy/CCM fixes"
  run bash "$SCRIPTS/selfmanaged-3-addons.sh"
}

stage_controller() {    # opt-in
  log "controller node — caps-self-md-ctrl (selfmanaged-4-controller.sh)"
  [[ "$DO_REPIN" == "1" ]] && info "DO_REPIN=1: will also repin Slurm + taint compute (disruptive)"
  run env DO_REPIN="$DO_REPIN" bash "$SCRIPTS/selfmanaged-4-controller.sh"
}

stage_slurm() {         # opt-in
  log "Slurm (slinky) stack — cert-manager + local-path + operator + slurm cluster"
  if [[ -f "$SCRIPTS/selfmanaged-5-slurm.sh" ]]; then
    info "note: caps-self-slurm.yaml pins slurmctld/slurmrestd to node-type=controller"
    info "and slurmd to node-type=compute — run WITH_CONTROLLER first (or label nodes)."
    run bash "$SCRIPTS/selfmanaged-5-slurm.sh"
  else
    info "scripts/selfmanaged-5-slurm.sh missing — cannot install Slurm."
  fi
}

# ── stage selection ─────────────────────────────────────────────────────────────
# Derive the ordered stage list from the flags, unless STAGES / `-- ...` overrides.
derive_stages() {
  local -a s=(networking)
  if [[ "$WORKER_MODE" == "flex" ]]; then
    s+=(fork flexvmss cluster workers dnslink wait addons)
  else
    s+=(cluster dnslink wait addons)
  fi
  [[ "$WITH_CONTROLLER" == "1" ]] && s+=(controller)
  [[ "$WITH_SLURM" == "1" ]]      && s+=(slurm)
  printf '%s\n' "${s[@]}"
}

main() {
  # Explicit stage override: STAGES env or args after `--`.
  local -a stages=()
  if [[ $# -gt 0 ]]; then
    # shellcheck disable=SC2206
    stages=(${*//,/ })
  elif [[ -n "${STAGES:-}" ]]; then
    # shellcheck disable=SC2206
    stages=(${STAGES//,/ })
  fi

  preflight

  if [[ ${#stages[@]} -eq 0 ]]; then
    mapfile -t stages < <(derive_stages)
  fi

  log "plan: ${stages[*]}"
  [[ "$PLAN" == "1" ]] && info "(PLAN=1 — nothing will be executed)"

  local st
  for st in "${stages[@]}"; do
    case "$st" in
      networking) stage_networking ;;
      fork)       stage_fork ;;
      flexvmss)   stage_flexvmss ;;
      cluster)    stage_cluster ;;
      workers)    stage_workers ;;
      dnslink)    stage_dnslink ;;
      wait)       stage_wait ;;
      addons)     stage_addons ;;
      controller) stage_controller ;;
      slurm)      stage_slurm ;;
      *) die "unknown stage: $st" ;;
    esac
  done

  log "done: ${stages[*]}"
  [[ "$PLAN" == "1" ]] || info "verify: kubectl --context $CTX get cluster,machine -A -o wide"
}

# Allow `selfmanaged-setup.sh -- networking cluster` style overrides.
if [[ "${1:-}" == "--" ]]; then shift; fi
main "$@"
