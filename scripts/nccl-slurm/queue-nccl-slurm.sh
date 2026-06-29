#!/usr/bin/env bash
# queue-nccl-slurm.sh — Slurm drop-in for queue-nccl-pbs.sh.
#
# Same positional arguments as the PBS version, so the existing callers
# (run-nccl-scales.sh / run-nccl-pairs.sh) work unchanged:
#   $0 <num nodes> <node list> <local=0|1> <test=allreduce|allgather|alltoall> \
#      <log dir> <sku> <vcpus> <gpu count> [benchmark target]
#
# INGESTION CONSISTENCY (the whole point): this preserves the PBS directory +
# output-filename conventions so process_nccl.sh and generate_telemetry_*.sh run
# UNCHANGED.
#   * raw dir   : ~/nccl_benchmarks_raw/<logdir>-nccl-<test>-<nodes>/   (identical)
#   * out file  : --output=%x.o%j  ->  "<jobname>.o<jobid>"             (identical
#                 pattern to PBS's "<jobname>.o<pbsjobid>", so the
#                 `sed 's/.*[^0-9]//g'` job-id extraction still works)
#
# Prints the submitted job id on stdout (sbatch --parsable) so an orchestrator
# can collect ids for an afterany gate + sacct exit-code check.
#
# Env knobs:
#   SUBMIT_WITH_NODELIST=1  pin the exact nodes (like PBS `-l nodes=...`). Default
#                           0: pass only --nodes=N and let Slurm/backfill place
#                           the job (better packing; compute nodes are fungible).
#   NCCL_NICE=<int>         Slurm --nice (largest-first packing; set by the
#                           orchestrator to size-derived value). Default 0.
#   NCCL_JOB_TIME=HH:MM:SS  per-job time limit (default 00:30:00; needed so the
#                           backfill scheduler can reason about gaps).
set -euo pipefail

if [ $# -lt 8 ]; then
    echo "Invalid usage: $0 <num nodes> <node list> <local=0|1> <test> <log dir> <sku> <vcpus> <gpu count> [benchmark target]" >&2
    exit 2
fi

NUM_NODES=$1
NODES_LIST=$2          # PBS form: "nodeA:ppn=96+nodeB:ppn=96"
LOCAL=$3
TEST=$4
LOGDIR=$5             # "scale" or "pair"
SKU=$6
CPUS=$7
DEVICES=$8
BENCHMARK_TARGET=${9:-}
SHARP=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# PBS node list -> Slurm CSV node list (strip ":ppn=NN", "+" -> ",")
NODES_CSV=$(printf '%s' "${NODES_LIST}" | sed 's/:ppn=[0-9]*//g; s/+/,/g')

# Job + directory names — IDENTICAL to queue-nccl-pbs.sh.
JOBNAME="${LOGDIR}-${NUM_NODES}-nccl"               # e.g. scale-2-nccl
LOGDIR_PREFIX="nccl-${TEST}"
LOGDIR="${LOGDIR}-${LOGDIR_PREFIX}-${NUM_NODES}"     # e.g. scale-nccl-allreduce-2
if [ "${LOCAL}" -eq 1 ]; then
    LOGDIR="${LOGDIR_PREFIX}-local"
fi

RAW_DIR="${HOME}/nccl_benchmarks_raw/${LOGDIR}"
mkdir -p "${RAW_DIR}"

# Optional exact-node pinning (PBS parity). Default off for better packing.
NODELIST_ARG=()
if [ "${SUBMIT_WITH_NODELIST:-0}" -eq 1 ]; then
    NODELIST_ARG=(--nodelist="${NODES_CSV}")
fi

# Submit. --parsable => prints just the job id. --output=%x.o%j reproduces the
# exact PBS "<jobname>.o<jobid>" filename inside RAW_DIR (via --chdir).
sbatch --parsable \
    --job-name="${JOBNAME}" \
    --nodes="${NUM_NODES}" \
    --ntasks-per-node="${DEVICES}" \
    "${NODELIST_ARG[@]}" \
    --exclusive \
    --nice="${NCCL_NICE:-0}" \
    --time="${NCCL_JOB_TIME:-00:30:00}" \
    --chdir="${RAW_DIR}" \
    --output="%x.o%j" \
    --export="ALL,NUM_NODES=${NUM_NODES},SHARP=${SHARP},NODES_LIST=${NODES_LIST},LOCAL=${LOCAL},TEST=${TEST},SKU=${SKU},DEVICES=${DEVICES},BENCHMARK_TARGET=${BENCHMARK_TARGET}" \
    "${SCRIPT_DIR}/run-nccl.slurm" | cut -d';' -f1
