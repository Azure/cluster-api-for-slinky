#!/usr/bin/env bash
#
# demo-presentation.sh - SELF-NARRATING live demo driver for the team progress
# presentation. Around each section's raw command output it prints:
#   * context   (what you're looking at + why it matters)
#   * watch      (the specific things to point at on screen)
#   * takeaway   (the one-line "so what")
# so the screen tells the story and you have to narrate very little.
#
# Runs from this workstation (Cloud PC); each step shells into the CAPZ mgmt VM
# via azure-remote.sh. Sections are independent so you can pace the talk:
#
#   ./demo-presentation.sh clusters   what Cluster API provisioned (mgmt view)
#   ./demo-presentation.sh nodes      workload nodes (1 CP + 2 V100/IB workers)
#   ./demo-presentation.sh slurm      Slurm scheduler + nodes idle/ready
#   ./demo-presentation.sh job        cross-node OSU MPI latency (HPC-X / TCP)
#   ./demo-presentation.sh bw         cross-node OSU MPI bandwidth (HPC-X / TCP)
#   ./demo-presentation.sh all        every section, top to bottom
#
# Live pacing: set PAUSE=1 to wait for <Enter> between sections, e.g.
#   PAUSE=1 ./demo-presentation.sh all
#
# Tip: run `clusters/nodes/slurm` while you talk, then `job` for the wow moment.
set -euo pipefail

REMOTE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/azure-remote.sh"
SECTION="${1:-all}"

# --- narration helpers (colors degrade gracefully if not a TTY) -------------
if [[ -t 1 ]]; then
  C_HDR=$'\033[1;36m'; C_DIM=$'\033[2m'; C_LOOK=$'\033[0;33m'; C_TAKE=$'\033[1;32m'; C_RST=$'\033[0m'
else
  C_HDR=''; C_DIM=''; C_LOOK=''; C_TAKE=''; C_RST=''
fi
hr()    { printf '\n%b=== %s ===%b\n' "$C_HDR" "$1" "$C_RST"; }
say()   { printf '%b  %s%b\n' "$C_DIM"  "$1" "$C_RST"; }   # context line(s)
watch() { printf '%b  watch: %s%b\n'    "$C_LOOK" "$1" "$C_RST"; }
take()  { printf '%b  takeaway: %s%b\n' "$C_TAKE" "$1" "$C_RST"; }
rule()  { printf '%b  %s%b\n' "$C_DIM" '----------------------------------------------------------------' "$C_RST"; }
pause() { [[ "${PAUSE:-0}" == "1" ]] && { printf '\n%b  [Enter to continue]%b' "$C_DIM" "$C_RST"; read -r _; }; return 0; }

clusters() {
  hr "1/5  Cluster API view - what got provisioned on Azure"
  say "This is the MANAGEMENT cluster describing the workload cluster 'caps-self'."
  say "Cluster API (CAPZ) declaratively built the Azure infra: control-plane VM +"
  say "a worker VM scale set. No portal clicks - it reconciles to the YAML manifest."
  rule
  bash "$REMOTE" ssh 'CTX=$(kubectl config current-context); kubectl --context "$CTX" get cluster,machinepool,machine -A'
  rule
  watch "Cluster PHASE=Provisioned; MachinePool 2/2 Running; 3 Machines Ready (1 CP + 2 workers); k8s v1.36.1."
  take  "Self-managed K8s control plane + worker VMSS, fully reconciled by Cluster API."
}

nodes() {
  hr "2/5  Workload nodes - the actual HPC hardware"
  say "Now talking to the WORKLOAD cluster itself. Workers are Standard_ND40rs_v2:"
  say "8x NVIDIA V100 GPU + 100Gb EDR InfiniBand, on the ubuntu-hpc image"
  say "(HPC-X, Mellanox OFED, CUDA/NCCL pre-baked). Same image family the team ships."
  rule
  bash "$REMOTE" ssh '
    KC=/tmp/caps-self.kubeconfig
    kubectl get secret caps-self-kubeconfig -n default -o jsonpath="{.data.value}" | base64 -d > "$KC"
    kubectl --kubeconfig "$KC" get nodes -o wide'
  rule
  watch "All STATUS=Ready; private 10.1.x IPs only (no public IPs - internal LB over VNet peering); containerd 2.3.0."
  take  "Real GPU + InfiniBand HPC nodes, managed as ordinary Kubernetes nodes."
}

slurm() {
  hr "3/5  Slurm scheduler (slinky operator) on Kubernetes"
  say "Slurm gives the team's familiar HPC scheduler, running as pods: slurmctld"
  say "(the controller) + one slurmd per HPC worker. srun/sbatch talk straight to"
  say "the controller. This is the PBS->Slurm story - same muscle memory."
  rule
  bash "$REMOTE" ssh '
    KC=/tmp/caps-self.kubeconfig
    kubectl --kubeconfig "$KC" get pods -n slurm -l "app.kubernetes.io/name in (slurmctld,slurmd)"
    echo
    CTRL=$(kubectl --kubeconfig "$KC" get pods -n slurm -l app.kubernetes.io/name=slurmctld -o name | head -1)
    kubectl --kubeconfig "$KC" exec -n slurm $CTRL -- sinfo'
  rule
  watch "slurmctld + 2 slurmd pods Running; sinfo shows 2 nodes STATE=idle = ready to accept jobs."
  take  "A working Slurm cluster on K8s - ready to schedule real MPI work."
}

bench() {
  local b="$1" tail="$2"
  bash "$REMOTE" ssh "
    KC=/tmp/caps-self.kubeconfig
    POD=\$(kubectl --kubeconfig \"\$KC\" get pods -n slurm -l app.kubernetes.io/name=slurmd -o name | head -1)
    kubectl --kubeconfig \"\$KC\" exec -n slurm \$POD -- bash -lc 'source /opt/hpcx/hpcx-init.sh && hpcx_load && export UCX_TLS=tcp UCX_NET_DEVICES=eth0 && srun -N2 --ntasks-per-node=1 --mpi=pmix --export=ALL \$HPCX_OSU_DIR/${b} 2>/dev/null | ${tail}'"
}

job() {
  hr "4/5  LIVE cross-node MPI - OSU latency (HPC-X, launched by Slurm)"
  say "This runs the standard OSU micro-benchmark across BOTH nodes, launched by"
  say "'srun --mpi=pmix' (Slurm) using HPC-X / Open MPI. Transport is TCP today -"
  say "deliberately the proven path; the hardware also has InfiniBand (see takeaway)."
  rule
  bench osu_latency 'head -25'
  rule
  watch "~22-23 us small-message latency between the two nodes; grows with message size."
  take  "Genuine cross-node MPI through Slurm. TCP now (~22 us); flipping UCX_TLS=tcp->rc enables RDMA over the IB fabric - next step, no hardware change."
}

bw() {
  hr "5/5  LIVE cross-node MPI - OSU bandwidth (HPC-X, launched by Slurm)"
  say "Same launch path, measuring throughput instead of latency."
  rule
  bench osu_bw 'tail -10'
  rule
  watch "~3.1 GB/s peak over TCP."
  take  "TCP tops out near 3 GB/s; the 100Gb EDR IB fabric (RDMA) is the headroom story - ~4x+ once UCX_TLS=rc is enabled."
}

intro() {
  printf '%b\n  CAPS demo - self-managed CAPZ cluster + Slurm + HPC-X MPI on Azure\n  %s%b\n' \
    "$C_HDR" "1 control plane + 2x V100/InfiniBand workers - cross-node MPI through Slurm" "$C_RST"
}
outro() {
  printf '\n%b  Summary: Cluster API provisioned a self-managed K8s cluster on V100/IB\n' "$C_TAKE"
  printf '  hardware, running Slurm; we launched real cross-node MPI over TCP.\n'
  printf '  Next: RDMA over InfiniBand (UCX_TLS=rc) + NCCL on the 8x V100 nodes.%b\n' "$C_RST"
}

case "$SECTION" in
  clusters) clusters ;;
  nodes)    nodes ;;
  slurm)    slurm ;;
  job)      job ;;
  bw)       bw ;;
  all)      intro; pause; clusters; pause; nodes; pause; slurm; pause; job; pause; bw; outro ;;
  *) echo "usage: $0 {clusters|nodes|slurm|job|bw|all}   (set PAUSE=1 to step through)" >&2; exit 1 ;;
esac
