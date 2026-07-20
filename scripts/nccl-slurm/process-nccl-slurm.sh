#!/usr/bin/env bash
# process-nccl-slurm.sh — collect + format NCCL host-launch results for the dashboard.
#
# PORT of hpc-image-val2 headnode/process_nccl.sh + the NCCL slice of
# generate_telemetry_NVIDIA.sh, collapsed into ONE pass (Zheyu: "don't artificially
# split"). Consumes the raw host-launch output that submit-nccl-host.sh /
# slurm-host-mpi.sh tee to:
#     ~/nccl_benchmarks_raw/<logdir>/<jobname>.o<allocjobid>
# The PBS "<jobname>.o<jobid>" convention is preserved on purpose, so this parser
# is identical to the PBS one. For each raw file it writes:
#   1. RAW_OUT/<test>_<jobid>.log        — "<size> <busbw>" pairs (human/CSV).
#   2. TELEMETRY_OUT/<test>_<jobid>.json — tokustocluster.py NCCL schema, so the
#      existing validation dashboard (CommType_s="nccl") consumes it UNCHANGED.
#
# Optional upload (UPLOAD=1): pipes each JSON to tokustocluster.py with the
# REPORTING managed-identity client id (KUSTO_CLIENT_ID) — never hardcoded. That
# reporting identity is DISTINCT from the infra-creation UAMI Pulumi/CAPZ uses.
#
# WHY env-driven metadata (not IMDS): this runs on the CAPZ mgmt VM, OUTSIDE the
# workload cluster, so IMDS here returns the mgmt VM, not the GPU worker. SKU /
# IMAGE / LOCATION are passed in from submit-nccl-host.sh (which knows them).
#
# TODO(caps): per-worker VmId + HWInfo + multi-iteration (iteration_2..5 / _avg)
#   aggregation. The old generate_telemetry did per-node SSH + IMDS on the compute
#   nodes; deferred with the health-check work. Single iteration_1 busbw is what the
#   NCCL dashboard query reads, so it is enough for this first slice.
# TODO(caps): health checks + TLS scan / disableTLSScan placeholder — the reporting
#   upload path may later need the disableTLSScan behavior; left as a placeholder.
# TODO(caps/zheyu): confirm how the reporting MI is provisioned vs the infra UAMI
#   (KUSTO_CLIENT_ID plumbing) — see tokustocluster.py header.
set -uo pipefail

RAW_ROOT="${RAW_ROOT:-${HOME}/nccl_benchmarks_raw}"
RAW_OUT="${RAW_OUT:-${HOME}/nccl_benchmarks}"
TELEMETRY_OUT="${TELEMETRY_OUT:-${HOME}/nccl_telemetry}"

# Dashboard metadata (env-driven; see header). Defaults match the NDV2/V100 path.
SKU="${SKU:-Standard_ND40rs_v2}"
IMAGE="${IMAGE:-microsoft-dsvm:ubuntu-hpc:2404-v100:24.04.2026052501}"
LOCATION="${LOCATION:-}"
RESOURCE_GROUP="${RESOURCE_GROUP:-}"
COMM_TYPE="${COMM_TYPE:-nccl}"
# The dashboard ignores tiny message sizes (parity with process_nccl.sh).
MIN_MSG_SIZE="${MIN_MSG_SIZE:-16384}"

# Optional Kusto upload (reporting identity — MI client id, never hardcoded).
UPLOAD="${UPLOAD:-0}"
KUSTO_CLIENT_ID="${KUSTO_CLIENT_ID:-}"
KUSTO_CLUSTER="${KUSTO_CLUSTER:-}"
KUSTO_DB="${KUSTO_DB:-}"
KUSTO_TABLE="${KUSTO_TABLE:-ImagePerf}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKUSTO="${TOKUSTO:-$SCRIPT_DIR/tokustocluster.py}"

command -v jq >/dev/null 2>&1 || { echo "##[error]jq is required (apt-get install -y jq)." >&2; exit 1; }
mkdir -p "$RAW_OUT" "$TELEMETRY_OUT"

shopt -s nullglob
raw_files=("$RAW_ROOT"/*/*.o*)
if (( ${#raw_files[@]} == 0 )); then
    echo "##[warning]No raw NCCL output under $RAW_ROOT/*/*.o* — nothing to collect." >&2
    exit 0
fi

overall=0
for f in "${raw_files[@]}"; do
    echo "##[section]Processing ${f}"
    logdir="$(basename "$(dirname "$f")")"            # scale-nccl-<test>-<size>
    jobid="$(basename "$f" | sed 's/.*[^0-9]//g')"     # trailing allocjobid
    test="$(echo "$logdir" | cut -d'-' -f3)"           # allreduce|allgather|alltoall
    if [ -z "$test" ]; then
        echo "##[warning]could not derive test from '$logdir'; skipping $f" >&2
        overall=1
        continue
    fi

    case "$test" in
        allreduce) benchmark="all_reduce_perf" ;;
        allgather) benchmark="all_gather_perf" ;;
        alltoall)  benchmark="alltoall_perf" ;;
        *)         benchmark="${test}_perf" ;;
    esac

    # NCCL >=2.14 added a column, shifting busbw (parity with process_nccl.sh):
    # allreduce reports out-of-place busbw one column further right than the rest.
    nccl_version="$(grep -m1 'NCCL version' "$f" | sed 's/.*NCCL version //; s/+.*//' | tr -d '[:space:]')"
    nccl_minor="$(echo "$nccl_version" | cut -d'.' -f2)"
    if [[ -z "$nccl_minor" || "$nccl_minor" -ge 14 ]]; then
        busbw_field=$([ "$test" = "allreduce" ] && echo 12 || echo 11)
    else
        busbw_field=$([ "$test" = "allreduce" ] && echo 11 || echo 10)
    fi

    # 1) "<size> <busbw>" pairs for msg sizes >= MIN_MSG_SIZE (skip comment lines).
    log_out="${RAW_OUT}/${test}_${jobid}.log"
    sed '/^#/d' "$f" \
        | awk -v col="$busbw_field" -v min="$MIN_MSG_SIZE" \
              '($1 ~ /^[0-9]+$/ && $1 >= min) {print $1, $col}' \
        | tee "$log_out"

    if [ ! -s "$log_out" ]; then
        echo "##[warning]no parsed rows for ${test} (${f}); skipping telemetry" >&2
        overall=1
        continue
    fi

    # 2) tokustocluster.py NCCL telemetry JSON. communicator.data.iteration_1 is the
    #    [{size, busbw}, ...] array the dashboard reads (KQL: CommType_s="nccl").
    json_out="${TELEMETRY_OUT}/${test}_${jobid}.json"
    pairs_json="$(awk '{printf "%s{\"size\":%s,\"busbw\":%s}", (NR>1?",":""), $1, $2}' "$log_out")"
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Processes = launched ranks; parse the nccl-tests rank banner, else env override.
    processes="${PROCESSES:-$(grep -c 'Rank .* device' "$f" 2>/dev/null || true)}"

    jq -n \
        --arg rg "$RESOURCE_GROUP" \
        --arg ts "$timestamp" \
        --arg comm "$COMM_TYPE" \
        --arg testtype "$test" \
        --arg benchmark "$benchmark" \
        --arg processes "$processes" \
        --arg image "$IMAGE" \
        --arg location "$LOCATION" \
        --arg vmtype "$SKU" \
        --arg version "$nccl_version" \
        --argjson pairs "[$pairs_json]" \
        '{
            ResourceGroup: $rg,
            Timestamp: $ts,
            CommType: $comm,
            TestType: $testtype,
            Benchmark: $benchmark,
            Processes: $processes,
            Image: $image,
            Location: $location,
            VmType: $vmtype,
            VmId: "",
            HWInfo: "{}",
            communicator: { version: $version, data: { iteration_1: $pairs } }
        }' | tee "$json_out"

    if [ "$UPLOAD" = "1" ]; then
        if [ -z "$KUSTO_CLUSTER" ] || [ -z "$KUSTO_DB" ]; then
            echo "##[warning]UPLOAD=1 but KUSTO_CLUSTER/KUSTO_DB unset; skipping upload of $json_out" >&2
        else
            command -v python3 >/dev/null 2>&1 || { echo "##[error]python3 required for upload." >&2; exit 1; }
            client_arg=()
            [ -n "$KUSTO_CLIENT_ID" ] && client_arg=(--client-id "$KUSTO_CLIENT_ID")
            if python3 "$TOKUSTO" "$KUSTO_CLUSTER" "$KUSTO_DB" "$KUSTO_TABLE" "$json_out" "${client_arg[@]}"; then
                echo "##[info]Uploaded $json_out to ${KUSTO_CLUSTER}/${KUSTO_DB}/${KUSTO_TABLE}"
            else
                echo "##[error]Kusto upload failed for $json_out" >&2
                overall=1
            fi
        fi
    fi
done

if [ "$overall" -ne 0 ]; then
    echo "##[warning]NCCL result collection completed with warnings/failures."
fi
echo "##[info]NCCL results: $RAW_OUT (busbw pairs) + $TELEMETRY_OUT (dashboard JSON)."
exit "$overall"
