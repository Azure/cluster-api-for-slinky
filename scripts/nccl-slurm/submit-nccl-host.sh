#!/usr/bin/env bash
# submit-nccl-host.sh — host-launch NCCL orchestrator (the proven IB path).
#
# The host-launch counterpart of submit-nccl-slurm.sh. Where that one uses the
# in-container sbatch model (and check_slurm_exit_codes/sacct for failure
# detection), THIS one drives scripts/slurm-host-mpi.sh — Slurm ALLOCATES the
# nodes, then run-nccl-host.sh runs mpirun on the worker HOST over SSH. That is
# the only path where caps-self cross-node RDMA is proven (rc_mlx5).
#
# It keeps the three behaviours from the migration brief:
#   1. RUN ONCE PER SUB-SCALE: one job per power-of-2 size (2,4,...,MAX_NODES) per
#      collective, not the whole-cluster partition sweep.
#   2. LARGEST-FIRST: sizes run in DESCENDING order (16-node, then 8, ... then 2).
#      NB: host-launch is SYNCHRONOUS per allocation (salloc --no-shell then
#      mpirun-over-SSH), so unlike the sbatch model there is no backfill
#      co-scheduling — descending order realizes "largest scale first" by
#      EXECUTION order, which is the meaningful part on a small cluster where big
#      jobs can't overlap anyway.
#   3. FAILURE DETECTION (PR 740 analog, host-launch flavour): slurm-host-mpi.sh
#      now exits with the REMOTE mpirun exit code; we capture it per job and fail
#      the whole run (exit 1) if any job did not return 0. (sacct can't help here
#      because the Slurm job is only the allocation, not the mpirun.)
#
# INGESTION: each run's raw stdout is tee'd (by slurm-host-mpi.sh via OUT_FILE) to
#   ~/nccl_benchmarks_raw/<logdir>/<jobname>.o<allocjobid>
# reproducing the PBS filename convention, so process-nccl-slurm.sh (the CAPS port
# of process_nccl.sh + the NCCL telemetry builder) consumes it unchanged. Pass
# COLLECT=1 to run that collector automatically after the sweep.
#
# Usage (run ON the CAPZ mgmt VM, same context as slurm-host-mpi.sh):
#   bash scripts/azure-remote.sh ssh \
#     'cd "$REMOTE_REPO" && bash scripts/nccl-slurm/submit-nccl-host.sh [max_nodes] [sku] [devices] "[collectives]"'
#
# Env:
#   UCX_TLS=rc,sm,self         IB by default; set tcp for the Ethernet fallback.
#   UCX_NET_DEVICES=mlx5_ib0:1 HCA (IB) or eth0 (TCP).
#   HOST_MPI=<path>            override the slurm-host-mpi.sh path.
#   COLLECT=1                  after the sweep, format results for the dashboard via
#                              process-nccl-slurm.sh (busbw pairs + tokustocluster JSON).
#                              UPLOAD=1 + KUSTO_* env additionally ingest to Kusto.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_MPI="${HOST_MPI:-$SCRIPT_DIR/../slurm-host-mpi.sh}"
RUN_NCCL_HOST="$SCRIPT_DIR/run-nccl-host.sh"

MAX_NODES="${1:-2}"
SKU="${2:-Standard_ND40rs_v2}"
DEVICES="${3:-8}"
TESTS="${4:-allreduce allgather alltoall}"

# InfiniBand transport by default (proven on caps-self); override for TCP.
export UCX_TLS="${UCX_TLS:-rc,sm,self}"
export UCX_NET_DEVICES="${UCX_NET_DEVICES:-mlx5_ib0:1}"

[ -f "$HOST_MPI" ]       || { echo "slurm-host-mpi.sh not found at $HOST_MPI" >&2; exit 1; }
[ -f "$RUN_NCCL_HOST" ]  || { echo "run-nccl-host.sh not found at $RUN_NCCL_HOST" >&2; exit 1; }

# Descending power-of-2 sizes <= MAX_NODES (largest first).
sizes=()
s=2
while (( s <= MAX_NODES )); do sizes+=("$s"); s=$(( s * 2 )); done
(( ${#sizes[@]} > 0 )) || sizes=(1)          # single-node cluster
desc=()
for (( i=${#sizes[@]}-1; i>=0; i-- )); do desc+=("${sizes[$i]}"); done

results=()
overall=0
echo "##[group]NCCL host-launch: MAX_NODES=$MAX_NODES sku=$SKU devices=$DEVICES UCX_TLS=$UCX_TLS"
for size in "${desc[@]}"; do
    for test in ${TESTS}; do
        jobname="scale-${size}-nccl"
        logdir="scale-nccl-${test}-${size}"
        raw_dir="${HOME}/nccl_benchmarks_raw/${logdir}"
        echo "##[section]NCCL test=${test} size=${size} (sku=${SKU})"

        NODES="$size" \
        NTASKS_PER_NODE="$DEVICES" \
        LAUNCHER="$RUN_NCCL_HOST" \
        OUT_FILE="${raw_dir}/${jobname}.o%j" \
        LAUNCH_EXTRA_ENV="TEST=${test} DEVICES=${DEVICES} SKU=${SKU} NUM_NODES=${size} LOCAL=0 SHARP=0 BENCHMARK_TARGET=${BENCHMARK_TARGET:-}" \
            bash "$HOST_MPI"
        rc=$?

        if [ "$rc" -eq 0 ]; then
            results+=("OK    test=${test} size=${size}")
            echo "##[info]NCCL test=${test} size=${size} PASSED (rc=0)"
        else
            results+=("FAIL  test=${test} size=${size} rc=${rc}")
            echo "##[error]NCCL test=${test} size=${size} FAILED (rc=${rc})"
            overall=1
        fi
    done
done
echo "##[endgroup]"

echo "##[section]NCCL host-launch summary"
for r in "${results[@]}"; do echo "  ${r}"; done

# Opt-in: collect + format the raw output for the validation dashboard. Runs
# regardless of pass/fail (partial results still help debugging). The benchmark
# pass/fail gate below stays authoritative; a collector hiccup only warns.
if [ "${COLLECT:-0}" = "1" ]; then
    SKU="$SKU" bash "$SCRIPT_DIR/process-nccl-slurm.sh" \
        || echo "##[warning]result collection reported problems (see above)."
fi

if [ "$overall" -ne 0 ]; then
    echo "##[error]One or more NCCL benchmarks failed."
    exit 1
fi
echo "##[info]All NCCL benchmarks completed successfully."
