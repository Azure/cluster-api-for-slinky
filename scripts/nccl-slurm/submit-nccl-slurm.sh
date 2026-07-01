#!/usr/bin/env bash
# submit-nccl-slurm.sh — heuristically-packed, run-once-per-scale NCCL submission.
#
# Replaces the PBS "scale" loop (run-nccl-scales.sh + generate-group.py) and the
# qstat-drain + check_pbs_exit_codes tail of benchmark_nccl.sh. Implements the
# three behaviour changes requested for the Slurm migration:
#
#  1. RUN ONCE PER SUB-SCALE. For an N-node cluster it submits ONE job per
#     power-of-2 size (2,4,...,N) per collective — e.g. 16 nodes -> one 2-node,
#     one 4-node, one 8-node, one 16-node job per collective (4 each), instead of
#     generate-group.py's whole-cluster partition sweep (8+4+2+1 = 15 each).
#
#  2. HEURISTIC PACKING (largest-first). Sizes are submitted in DESCENDING order
#     and each job gets a size-derived `--nice` (larger size => lower nice =>
#     higher priority). Slurm runs the big jobs first; its backfill scheduler
#     then co-schedules the smaller ones into the gaps (8+4+2 = 14 <= 16). MPI
#     jobs are interleaved at each size so packing spans NCCL *and* MPI.
#
#  3. FAILURE DETECTION (PR 740 analog). Every job id is collected; an
#     `afterany` gate blocks until all reach a terminal state; then
#     check_slurm_exit_codes() verifies State+ExitCode via sacct and fails the
#     run (exit 1) if any job did not COMPLETE 0:0.
#
# Usage:
#   submit-nccl-slurm.sh [hostfile] [sku] [vcpus] [gpu_count] ["collectives"]
# Defaults target the caps-self ND96asr_v4 (A100) plan; override as needed.
#
# Env:
#   MPI_BENCH=/path/to/mpi.slurm   co-pack an MPI benchmark job at each size.
#   USE_ACCOUNTING=0               use the no-accounting (scontrol) check path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common-slurm.sh
source "${SCRIPT_DIR}/common-slurm.sh"

# Default SKU = the caps-self hardware in play (ND40rs_v2: 8x V100 + 100Gb EDR IB;
# 40 vCPUs). NOTE: cross-node IB RDMA on caps-self is proven only via the
# HOST-LAUNCH path (submit-nccl-host.sh); this in-container sbatch path currently
# exercises the TCP transport (run-nccl.slurm's ND40rs_v2 branch is TCP-tuned).
HOSTFILE="${1:-$HOME/benchmark_scripts/compute_ccl_mpi.txt}"
SKU="${2:-Standard_ND40rs_v2}"
CPUS="${3:-40}"
DEVICES="${4:-8}"
TESTS="${5:-allreduce allgather alltoall}"
MPI_BENCH="${MPI_BENCH:-}"

[ -r "${HOSTFILE}" ] || { echo "hostfile not readable: ${HOSTFILE}" >&2; exit 1; }
mapfile -t HOSTS < <(grep -vE '^[[:space:]]*$' "${HOSTFILE}")
N=${#HOSTS[@]}
(( N >= 1 )) || { echo "no hosts in ${HOSTFILE}" >&2; exit 1; }

# Power-of-2 sizes 2..N, then reverse to descending (largest first).
sizes=()
s=2
while (( s <= N )); do sizes+=("${s}"); s=$(( s * 2 )); done
(( ${#sizes[@]} > 0 )) || sizes=(1)          # single-node cluster
desc=()
for (( i=${#sizes[@]}-1; i>=0; i-- )); do desc+=("${sizes[$i]}"); done
max_size=${desc[0]}

# Build a PBS-form node list of the first <count> hosts (queue-nccl-slurm.sh
# converts it to a Slurm nodelist; only used when SUBMIT_WITH_NODELIST=1).
build_pbs_nodelist() {
    local n=$1 out="" i
    for (( i=0; i<n; i++ )); do out+="${HOSTS[$i]}:ppn=${CPUS}+"; done
    printf '%s' "${out%+}"
}

ids=()
echo "##[group]Submitting NCCL${MPI_BENCH:+/MPI} jobs largest-first (N=${N})"
for size in "${desc[@]}"; do
    # Largest-first: size==max_size => nice 0 (top priority); smaller => higher nice.
    export NCCL_NICE=$(( (max_size - size) * 1000 ))
    nodelist="$(build_pbs_nodelist "${size}")"

    for test in ${TESTS}; do
        jid=$(bash "${SCRIPT_DIR}/queue-nccl-slurm.sh" \
                  "${size}" "${nodelist}" 0 "${test}" scale "${SKU}" "${CPUS}" "${DEVICES}" "")
        echo "  NCCL  test=${test} size=${size} nice=${NCCL_NICE} jobid=${jid}"
        ids+=("${jid}")
    done

    if [ -n "${MPI_BENCH}" ]; then
        jid=$(sbatch --parsable \
                  --job-name="mpi-${size}" \
                  --nodes="${size}" --ntasks-per-node="${CPUS}" \
                  --exclusive --nice="${NCCL_NICE}" \
                  --time="${NCCL_JOB_TIME:-00:30:00}" \
                  --export="ALL,SCALE=${size},CPUS=${CPUS},DEVICES=${DEVICES},SKU=${SKU}" \
                  "${MPI_BENCH}" | cut -d';' -f1)
        echo "  MPI   size=${size} nice=${NCCL_NICE} jobid=${jid}"
        ids+=("${jid}")
    fi
done
echo "##[endgroup]"

ids_csv="$(IFS=,; printf '%s' "${ids[*]}")"
ids_colon="$(IFS=:; printf '%s' "${ids[*]}")"

echo "##[section]Waiting for ${#ids[@]} jobs to drain (afterany gate)"
# Gate job depends on ALL benchmark jobs (afterany fires regardless of pass/fail);
# `--wait` blocks here until the gate — hence every dependency — has finished.
sbatch --wait --dependency="afterany:${ids_colon}" \
       --job-name=nccl-gate --nodes=1 --time=00:05:00 \
       --wrap='echo "all NCCL/MPI jobs reached a terminal state"' >/dev/null || true
echo "##[info]All jobs finished running!"

# PR 740 analog: fail the run if any job did not COMPLETE 0:0.
if [ "${USE_ACCOUNTING:-1}" -eq 1 ]; then
    check_slurm_exit_codes "NCCL" "${ids_csv}" || { echo "##[error]NCCL/MPI benchmark failure detected."; exit 1; }
else
    wait_and_check_jobs_no_acct "NCCL" "${ids_csv}" || { echo "##[error]NCCL/MPI benchmark failure detected."; exit 1; }
fi

echo "##[info]All NCCL/MPI jobs completed successfully."
