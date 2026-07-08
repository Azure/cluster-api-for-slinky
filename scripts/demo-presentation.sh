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
#   ./demo-presentation.sh nodes      workload nodes (1 CP + 3 workers: 2 V100/IB compute + controller)
#   ./demo-presentation.sh slurm      Slurm scheduler + nodes idle/ready
#   ./demo-presentation.sh job        cross-node OSU MPI latency (HPC-X, TCP vs RDMA)
#   ./demo-presentation.sh bw         cross-node OSU MPI bandwidth over RDMA (HPC-X)
#   ./demo-presentation.sh nccl       multi-GPU NCCL all-reduce, host-launch over IB
#   ./demo-presentation.sh all        every section, top to bottom
#
# Live pacing: set PAUSE=1 to wait for <Enter> between sections, e.g.
#   PAUSE=1 ./demo-presentation.sh all
#
# Tip: run `clusters/nodes/slurm` while you talk, then `job` for the wow moment.
set -euo pipefail

REMOTE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/azure-remote.sh"
SECTION="${1:-all}"

# UCX transports for the MPI benchmarks (override via env if device names differ).
#   TCP  = ordinary Ethernet (eth0).  RDMA = rc verbs over the InfiniBand NIC.
UCX_TCP="${UCX_TCP:-UCX_TLS=tcp UCX_NET_DEVICES=eth0}"
UCX_RDMA="${UCX_RDMA:-UCX_TLS=rc UCX_NET_DEVICES=mlx5_ib0:1}"

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
  hr "1/6  Cluster API view - what got provisioned on Azure"
  say "This is the MANAGEMENT cluster describing the workload cluster 'caps-self'."
  say "Cluster API (CAPZ) declaratively built the Azure infra: a control-plane VM +"
  say "MachineDeployment worker VMs. No portal clicks - it reconciles to the manifest."
  rule
  bash "$REMOTE" ssh 'CTX=$(kubectl config current-context); kubectl --context "$CTX" get cluster,machinedeployment,machine -A'
  rule
  watch "Cluster PHASE=Provisioned; all Machines Ready (1 CP + 3 workers: 2 GPU compute + 1 Slurm-controller); k8s v1.36.1."
  take  "Self-managed K8s control plane + worker MachineDeployments, fully reconciled by Cluster API."
}

nodes() {
  hr "2/6  Workload nodes - the actual HPC hardware"
  say "Now talking to the WORKLOAD cluster itself. The 2 compute nodes are"
  say "Standard_ND40rs_v2: 8x NVIDIA V100 GPU + 100Gb EDR InfiniBand, on the"
  say "ubuntu-hpc image (HPC-X, Mellanox OFED, CUDA/NCCL pre-baked). A separate"
  say "smaller node hosts the Slurm controller. Same image family the team ships."
  rule
  bash "$REMOTE" ssh '
    KC=/tmp/caps-self.kubeconfig
    kubectl get secret caps-self-kubeconfig -n default -o jsonpath="{.data.value}" | base64 -d > "$KC"
    kubectl --kubeconfig "$KC" get nodes -o wide -L slinky.slurm.net/node-type -L node.kubernetes.io/instance-type'
  rule
  watch "node-type=compute -> the 2 ND40rs_v2 GPU/IB nodes; all Ready; private 10.1.x IPs only (internal LB over VNet peering)."
  take  "Real GPU + InfiniBand HPC nodes, managed as ordinary Kubernetes nodes."
}

slurm() {
  hr "3/6  Slurm scheduler (slinky operator) on Kubernetes"
  say "Slurm gives the team's familiar HPC scheduler, running as pods: slurmctld"
  say "(the controller, on its own node) + one slurmd per GPU compute node."
  say "srun/sbatch talk straight to the controller. This is the PBS->Slurm story."
  rule
  bash "$REMOTE" ssh '
    KC=/tmp/caps-self.kubeconfig
    kubectl --kubeconfig "$KC" get pods -n slurm -o wide -l "app.kubernetes.io/name in (slurmctld,slurmd)"
    echo
    CTRL=$(kubectl --kubeconfig "$KC" get pods -n slurm -l app.kubernetes.io/name=slurmctld -o name | head -1)
    kubectl --kubeconfig "$KC" exec -n slurm $CTRL -- sinfo'
  rule
  watch "slurmctld + 2 slurmd pods Running; sinfo shows 2 compute nodes STATE=idle = ready to accept jobs."
  take  "A working Slurm cluster on K8s - ready to schedule real MPI work."
}

bench() {
  # $1 = osu binary, $2 = output filter, $3 = UCX env (default RDMA/InfiniBand)
  local b="$1" tail="$2" ucx="${3:-$UCX_RDMA}"
  bash "$REMOTE" ssh "
    KC=/tmp/caps-self.kubeconfig
    POD=\$(kubectl --kubeconfig \"\$KC\" get pods -n slurm -l app.kubernetes.io/name=slurmd -o name | head -1)
    kubectl --kubeconfig \"\$KC\" exec -n slurm \$POD -- bash -lc 'source /opt/hpcx/hpcx-init.sh && hpcx_load && export ${ucx} && srun -N2 --ntasks-per-node=1 --mpi=pmix --export=ALL \$HPCX_OSU_DIR/${b} 2>/dev/null | ${tail}'"
}

job() {
  hr "4/6  LIVE cross-node MPI - OSU latency: TCP vs RDMA (same hardware)"
  say "Standard OSU latency across BOTH nodes via 'srun --mpi=pmix' (HPC-X/Open MPI)."
  say "Same job, run twice: first over TCP (eth0), then over RDMA (rc verbs on the"
  say "InfiniBand NIC). Only the UCX transport changes - identical nodes and launch."
  rule
  printf '%b  -- TCP  (UCX_TLS=tcp, eth0) --%b\n' "$C_DIM" "$C_RST"
  bench osu_latency 'head -25' "$UCX_TCP"
  printf '%b  -- RDMA (UCX_TLS=rc, InfiniBand) --%b\n' "$C_DIM" "$C_RST"
  bench osu_latency 'head -25' "$UCX_RDMA"
  rule
  watch "small-message latency: TCP ~22 us vs RDMA ~1.8 us - same nodes, only the transport changed."
  take  "RDMA over InfiniBand: ~12x lower latency than TCP. This is the payoff of the V100 + EDR-IB SKU."
}

bw() {
  hr "5/6  LIVE cross-node MPI - OSU bandwidth over RDMA (InfiniBand)"
  say "Same launch path, measuring throughput over RDMA. (TCP peaked near 3 GB/s.)"
  rule
  bench osu_bw 'tail -12' "$UCX_RDMA"
  rule
  watch "~10-11 GB/s peak over the 100Gb EDR InfiniBand fabric."
  take  "RDMA delivers ~3.5x the TCP bandwidth (~11 vs ~3 GB/s) - near line-rate on 100Gb EDR."
}

nccl() {
  hr "6/6  LIVE multi-GPU NCCL all-reduce - host-launch over InfiniBand"
  say "The GPU collective that HPC/AI training depends on. 16x V100 (8 per node),"
  say "NCCL all-reduce over InfiniBand. HOST-LAUNCH model: Slurm ALLOCATES the two"
  say "nodes, then mpirun runs on the worker HOST over SSH (native /dev/infiniband)."
  rule
  bash "$REMOTE" ssh 'cd ~/caps-pulumi && bash scripts/nccl-slurm/submit-nccl-host.sh 2 Standard_ND40rs_v2 8 "allreduce" 2>&1 | grep -vE "^#[[:space:]]+Rank|nThread|Using devices|Warning|hcoll|bootstrapping" | tail -50'
  rule
  watch "busbw column climbs with message size to ~15.7 GB/s peak; #wrong=0 (correct); PASSED."
  take  "Real multi-node GPU NCCL all-reduce over InfiniBand - the collective HPC/AI training scales on."
}

intro() {
  printf '%b\n  CAPS demo - self-managed CAPZ cluster + Slurm + HPC-X MPI/NCCL on Azure\n  %s%b\n' \
    "$C_HDR" "2x 8-GPU V100/InfiniBand nodes - cross-node MPI + multi-GPU NCCL via Slurm" "$C_RST"
}
outro() {
  printf '\n%b  Summary: Cluster API provisioned a self-managed K8s cluster on V100/IB\n' "$C_TAKE"
  printf '  hardware, running Slurm; we launched cross-node MPI over RDMA\n'
  printf '  (~1.8 us, ~11 GB/s) AND multi-GPU NCCL all-reduce over IB (~15.7 GB/s).\n'
  printf '  Next: scale to more nodes + port the validation pipeline workloads.%b\n' "$C_RST"
}

case "$SECTION" in
  clusters) clusters ;;
  nodes)    nodes ;;
  slurm)    slurm ;;
  job)      job ;;
  bw)       bw ;;
  nccl)     nccl ;;
  all)      intro; pause; clusters; pause; nodes; pause; slurm; pause; job; pause; bw; pause; nccl; outro ;;
  *) echo "usage: $0 {clusters|nodes|slurm|job|bw|nccl|all}   (set PAUSE=1 to step through)" >&2; exit 1 ;;
esac
