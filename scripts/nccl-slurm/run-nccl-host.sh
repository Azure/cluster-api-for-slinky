#!/bin/bash
# run-nccl-host.sh — HOST-namespace NCCL launcher (host-launch model).
#
# The counterpart of run-nccl.slurm for the *host-launch* path: instead of running
# inside the slurmd container under sbatch, this is fed to a worker HOST over SSH by
# scripts/slurm-host-mpi.sh (`LAUNCHER=.../run-nccl-host.sh`), exactly like
# scripts/ib-osu-loopback.sh. Slurm only ALLOCATES the nodes; mpirun runs on the
# worker VM with native /dev/infiniband access — which is the ONLY path on caps-self
# where cross-node RDMA is proven (osu ~1.67us / ~11 GB/s over rc_mlx5).
#
# It reads the same launcher contract as ib-osu-loopback.sh (HOSTS / UCX_TLS /
# UCX_NET_DEVICES) plus NCCL knobs passed via slurm-host-mpi.sh's LAUNCH_EXTRA_ENV
# (TEST / DEVICES / SKU / NUM_NODES / LOCAL / SHARP / BENCHMARK_TARGET).
#
# EXIT CODE: each SKU branch ends on `exec mpirun ... </dev/null`, so this script's
# exit code IS mpirun's. slurm-host-mpi.sh captures it and exits with it, so the
# orchestrator (submit-nccl-host.sh) can fail on a crashed run — the host-launch
# analog of PR 740's exit-code gate. The `</dev/null` is required because the script
# is piped in via `bash -s` (mpirun would otherwise consume the remaining stdin).
#
# NOT set -e: we want mpirun's status to be the exit code, not an early abort.
# NOT set -u either: HPC-X's hpcx-init.sh references unset vars (e.g.
# HPCX_ENABLE_NCCL_MRC_PLUGIN) and aborts under nounset when sourced (same reason
# ib-osu-loopback.sh runs without -u).
set -o pipefail

# --- load HPC-X from the host image (provides mpirun) ------------------------
HPCX_DIR=$(ls -d /opt/hpcx-v2* /opt/hpcx* 2>/dev/null | head -1)
if [ -n "$HPCX_DIR" ] && [ -f "$HPCX_DIR/hpcx-init.sh" ]; then
  # shellcheck disable=SC1090
  source "$HPCX_DIR/hpcx-init.sh"
  hpcx_load
else
  source /etc/profile.d/modules.sh 2>/dev/null || true
  module load mpi/hpcx 2>/dev/null || true
fi

NUM_NODES="${NUM_NODES:-2}"
DEVICES="${DEVICES:-8}"
TEST="${TEST:-allreduce}"
SKU="${SKU:-Standard_ND40rs_v2}"
LOCAL="${LOCAL:-0}"
SHARP="${SHARP:-0}"
BENCHMARK_TARGET="${BENCHMARK_TARGET:-}"
HOSTS="${HOSTS:-}"
UCX_TLS="${UCX_TLS:-rc,sm,self}"
UCX_NET_DEVICES="${UCX_NET_DEVICES:-mlx5_ib0:1}"
NCCL_TESTS_DIR="${NCCL_TESTS_DIR:-/opt/nccl-tests/build}"

echo "=== host: $(hostname) ==="
[ -n "$HOSTS" ] && echo "# HOSTS: $HOSTS"
echo "# mpirun: $(command -v mpirun || echo MISSING)"

# Placement: cross-node uses the discovered --host ip:slots string; np = nodes*gpus.
if [ -n "$HOSTS" ]; then PLACE="--host $HOSTS"; else PLACE=""; fi
NP=$(( NUM_NODES * DEVICES ))

SHARP_ARGS=""
if [ "$SHARP" -eq 1 ]; then
    SHARP_ARGS="-x NCCL_COLLNET_ENABLE=1 -x NCCL_ALGO=CollNet -x SHARP_COLL_ENABLE_SAT=1 \
    -x SHARP_COLL_LOG_LEVEL=3 -x NCCL_DEBUG_SUBSYS=INIT -x SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1 \
    -x SHARP_COLL_NUM_COLL_GROUP_RESOURCE_ALLOC_THRESHOLD=0 -x SHARP_COLL_LOCK_ON_COMM_INIT=1"
fi

MSG_START="8"
MSG_END="16G"
LOCAL_ARGS=""
if [ "$LOCAL" -eq 1 ]; then
    LOCAL_ARGS="-x NCCL_P2P_DISABLE=1 -x NCCL_SHM_DISABLE=1"
    MSG_START="4G"
    MSG_END="4G"
fi

case ${TEST} in
    "allreduce") TEST="all_reduce_perf" ;;
    "allgather") TEST="all_gather_perf" ;;
    "alltoall")  TEST="alltoall_perf" ;;
    *) echo "Error! Incorrect test provided"; exit 1 ;;
esac

# Locate the nccl-tests binary (env-overridable, with a find fallback).
BIN="${NCCL_TESTS_DIR}/${TEST}"
if [ ! -x "$BIN" ]; then
    BIN=$(find "$NCCL_TESTS_DIR" /opt/nccl-tests /opt -name "$TEST" -type f 2>/dev/null | head -1)
fi
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
    echo "ERROR: NCCL test binary '${TEST}' not found (looked under ${NCCL_TESTS_DIR})." >&2
    exit 1
fi

# Transport-aware NCCL data-path args. NCCL uses its OWN IB verbs (NCCL_IB_HCA),
# independent of UCX (which only bootstraps MPI); UCX_TLS selects the mode here.
if [[ "$UCX_TLS" == *rc* ]]; then
    NCCL_NET_ARGS="-x NCCL_IB_HCA=${NCCL_IB_HCA:-mlx5_ib0} -x NCCL_IB_PCI_RELAXED_ORDERING=1 -x NCCL_SOCKET_IFNAME=eth0"
    echo "# transport: InfiniBand (NCCL_IB_HCA=${NCCL_IB_HCA:-mlx5_ib0}, UCX_TLS=$UCX_TLS, dev=$UCX_NET_DEVICES)"
else
    NCCL_NET_ARGS="-x NCCL_IB_DISABLE=1 -x NCCL_SOCKET_IFNAME=eth0"
    echo "# transport: TCP (NCCL_IB_DISABLE=1)"
fi

date
echo "# Test: ${TEST}"
echo "# Node count: ${NUM_NODES}"
echo "# HOSTS: ${HOSTS}"
echo "# SKU: ${SKU}"

case ${SKU} in
    "Standard_ND40rs_v2")
        # 8x V100 + 100Gb EDR InfiniBand. Transport chosen by UCX_TLS (rc* => IB,
        # else TCP). -b 1K -e 4G: V100 has 32GB HBM, so larger buffers OOM with -c 1.
        exec mpirun \
            -np "$NP" $PLACE \
            --map-by ppr:"$DEVICES":node \
            --bind-to numa \
            --timeout 1800 \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:"$LD_LIBRARY_PATH" \
            -mca coll_hcoll_enable 0 \
            -x UCX_TLS="$UCX_TLS" \
            -x UCX_NET_DEVICES="$UCX_NET_DEVICES" \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_DEBUG=WARN \
            ${NCCL_NET_ARGS} ${SHARP_ARGS} ${LOCAL_ARGS} \
            "$BIN" -b 1K -e 4G -f 2 -g 1 -c 1 -n 50 </dev/null
        ;;
    "Standard_ND96amsr_A100_v4"|"Standard_ND96asr_v4")
        # A100 + HDR IB. ndv4 topology file present on the ubuntu-hpc image.
        exec mpirun \
            -np "$NP" $PLACE \
            --map-by ppr:"$DEVICES":node \
            --bind-to numa \
            --timeout 1800 \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:"$LD_LIBRARY_PATH" \
            -mca coll_hcoll_enable 0 \
            -x UCX_TLS="$UCX_TLS" \
            -x UCX_NET_DEVICES="$UCX_NET_DEVICES" \
            -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            -x NCCL_DEBUG=WARN \
            ${SHARP_ARGS} ${LOCAL_ARGS} \
            -x NCCL_TOPO_FILE=/opt/microsoft/ndv4-topo.xml \
            "$BIN" -b "${MSG_START}" -e "${MSG_END}" -f 2 -g 1 -c 1 -n 50 </dev/null
        ;;
    "Standard_ND96isr_H100_v5"|"Standard_ND96isr_H200_v5")
        exec mpirun \
            -np "$NP" $PLACE \
            --map-by ppr:"$DEVICES":node \
            --bind-to numa \
            --timeout 1800 \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:"$LD_LIBRARY_PATH" \
            -mca coll_hcoll_enable 0 \
            -x UCX_TLS="$UCX_TLS" \
            -x UCX_NET_DEVICES="$UCX_NET_DEVICES" \
            -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            -x NCCL_DEBUG=WARN \
            ${SHARP_ARGS} ${LOCAL_ARGS} \
            "$BIN" -b "${MSG_START}" -e "${MSG_END}" -f 2 -g 1 -c 1 -n 50 </dev/null
        ;;
    "Standard_ND128isr_NDR_GB200_v6"|"Standard_ND128isr_GB300_v6")
        echo "# Benchmark target: ${BENCHMARK_TARGET}"
        case ${BENCHMARK_TARGET} in
            "NVLINK")
                exec mpirun \
                    -np "$NP" $PLACE --bind-to none --map-by ppr:"$DEVICES":node --timeout 1800 \
                    -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:"$LD_LIBRARY_PATH" \
                    -mca coll_hcoll_enable 0 \
                    -x NCCL_COLLNET_ENABLE=1 -x NCCL_CUMEM_ENABLE=1 -x NCCL_MNNVL_ENABLE=1 \
                    -x NCCL_SHM_DISABLE=0 -x NCCL_NET_GDR_C2C=0 \
                    -x SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1 -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
                    -x CUDA_DEVICE_ORDER=PCI_BUS_ID -x NCCL_SOCKET_IFNAME=eth0 -x NCCL_DEBUG=WARN \
                    ${SHARP_ARGS} ${LOCAL_ARGS} \
                    "$BIN" -b "${MSG_START}" -e "${MSG_END}" -f 2 -g 1 -c 1 -n 50 </dev/null
                ;;
            "INFINIBAND")
                exec mpirun \
                    -np "$NP" $PLACE --bind-to none --map-by ppr:"$DEVICES":node --timeout 1800 \
                    -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:"$LD_LIBRARY_PATH" \
                    -mca coll_hcoll_enable 0 \
                    -x NCCL_COLLNET_ENABLE=0 -x NCCL_CUMEM_ENABLE=0 -x NCCL_MNNVL_ENABLE=0 \
                    -x NCCL_SHM_DISABLE=1 -x NCCL_NET_GDR_C2C=1 \
                    -x SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1 -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
                    -x CUDA_DEVICE_ORDER=PCI_BUS_ID -x NCCL_SOCKET_IFNAME=eth0 -x NCCL_DEBUG=WARN \
                    ${SHARP_ARGS} ${LOCAL_ARGS} \
                    "$BIN" -b "${MSG_START}" -e "${MSG_END}" -f 2 -g 1 -c 1 -n 50 </dev/null
                ;;
            *) echo "Error! Incorrect benchmark target (${BENCHMARK_TARGET}) provided"; exit 1 ;;
        esac
        ;;
    *)
        echo "Error! SKU '${SKU}' is not wired for the host-launch (IB) path."
        echo "       Non-IB SKUs should use the in-container path (run-nccl.slurm)."
        exit 1
        ;;
esac
