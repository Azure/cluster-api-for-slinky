#!/usr/bin/env bash
#
# azure-remote.sh - drive the CAPZ management VM from a workstation.
#
# Why this exists: the `azure` Pulumi stack's management cluster runs on the
# Azure VM `vm-capz-mi-dev`, which has the user-assigned managed identity (UAMI)
# attached. A laptop / Cloud PC does NOT have that identity, so any kubectl /
# pulumi / clusterctl action that talks to CAPZ must run on the VM. This wraps
# rsync (push the local working tree) + ssh (run commands there) so you can edit
# locally and execute remotely.
#
# Connection details live in ~/.ssh/config under the `capz-mgmt` alias. Override
# with env vars:
#   CAPZ_REMOTE       ssh alias or user@host   (default: capz-mgmt)
#   CAPZ_REMOTE_REPO  repo path on the VM      (default: /home/azureuser/caps-pulumi)
#   CAPZ_CONTEXT      kube context on the VM   (default: auto-detected)
#   CAPZ_PULUMI_STACK Pulumi stack             (default: azurebyo)
#
# Usage:
#   azure-remote.sh sync                 rsync local working tree -> VM
#   azure-remote.sh ssh [cmd...]         interactive shell on the VM (or run cmd)
#   azure-remote.sh kubectl <args...>    kubectl against the mgmt cluster
#   azure-remote.sh preview              sync, then preview the configured Pulumi stack
#   azure-remote.sh up                   sync, then update the configured Pulumi stack
#   azure-remote.sh apply-selfmanaged    sync, then apply the self-managed manifest
#   azure-remote.sh delete-selfmanaged   delete the self-managed cluster CRs
#   azure-remote.sh addons               install Day-2 addons (cloud-provider-azure + Calico)
#   azure-remote.sh bridge-experiment [all|install|demo|teardown]  CPU-only slurm-bridge demo
#   azure-remote.sh watch                list CAPI/CAPZ workload-cluster CRs
#   azure-remote.sh describe             clusterctl describe cluster caps-self
set -euo pipefail

REMOTE="${CAPZ_REMOTE:-capz-mgmt}"
REMOTE_REPO="${CAPZ_REMOTE_REPO:-/home/azureuser/caps-pulumi}"
LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="selfmanaged-workload-cluster.yaml"
CONTEXT="${CAPZ_CONTEXT:-}"
PULUMI_STACK="${CAPZ_PULUMI_STACK:-azurebyo}"

ssh_run() { ssh "$REMOTE" "$@"; }

# Resolve the kube context once per invocation (the kind cluster is autonamed,
# so don't hard-code it). Cached in CONTEXT after the first lookup.
need_context() {
  [[ -n "$CONTEXT" ]] || CONTEXT="$(ssh_run 'kubectl config current-context')"
}

kube() { need_context; ssh_run "kubectl --context '$CONTEXT' $*"; }

sync_repo() {
  # Push code + manifests; preserve the VM's runtime state. NEVER --delete (it
  # would nuke VM-only artifacts such as generated kubeconfigs). Excludes:
  #   .state  - pulumi local-backend state (the VM is the source of truth)
  #   .venv   - platform-specific virtualenv
  #   .git    - leave the VM's own checkout/metadata alone
  # Note: '.git' (no slash) matches whether the local checkout uses a .git
  # directory or a gitlink file (worktree); a '.git/' dir-pattern would miss
  # the file form and clobber the VM's real .git directory.
  rsync -az \
    --exclude '.git' --exclude '.venv' --exclude '.state' \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
    "$LOCAL_REPO/" "$REMOTE:$REMOTE_REPO/"
}

usage() { sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

cmd="${1:-}"; shift || true
case "$cmd" in
  sync)
    sync_repo; echo "synced $LOCAL_REPO -> $REMOTE:$REMOTE_REPO" ;;
  ssh)
    if [[ $# -gt 0 ]]; then ssh -t "$REMOTE" "cd '$REMOTE_REPO' && $*"
    else ssh -t "$REMOTE" "cd '$REMOTE_REPO'; exec bash -l"; fi ;;
  kubectl)
    kube "$@" ;;
  preview)
    sync_repo; ssh_run "cd '$REMOTE_REPO/pulumi' && pulumi preview -s '$PULUMI_STACK'" ;;
  up)
    sync_repo; ssh_run "cd '$REMOTE_REPO/pulumi' && pulumi up -s '$PULUMI_STACK'" ;;
  apply-selfmanaged)
    sync_repo
    need_context
    echo ">> applying $MANIFEST against context $CONTEXT"
    kube "apply -f '$REMOTE_REPO/$MANIFEST'" ;;
  delete-selfmanaged)
    kube "delete -f '$REMOTE_REPO/$MANIFEST'" ;;
  addons)
    sync_repo
    echo ">> installing Day-2 addons (cloud-provider-azure + Calico) on caps-self"
    ssh_run "cd '$REMOTE_REPO' && bash scripts/selfmanaged-3-addons.sh" ;;
  bridge-experiment)
    sync_repo
    echo ">> running CPU-only slurm-bridge experiment on caps-self (${*:-all})"
    ssh_run "cd '$REMOTE_REPO' && bash scripts/bridge-experiment/run.sh $*" ;;
  watch)
    kube "get cluster,azurecluster,kubeadmcontrolplane,machinepool,azuremachinepool -A" ;;
  describe)
    ssh_run "clusterctl describe cluster caps-self 2>/dev/null || echo 'clusterctl not installed on the VM'" ;;
  ""|-h|--help|help)
    usage ;;
  *)
    echo "unknown command: $cmd" >&2; usage; exit 1 ;;
esac
