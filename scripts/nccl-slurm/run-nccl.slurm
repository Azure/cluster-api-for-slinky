#!/bin/bash
# run-nccl.slurm — Slurm drop-in for run-nccl.pbs.
#
# Body is byte-for-byte the PBS version EXCEPT two scheduler couplings:
#   1. HPC-X load: `module load mpi/hpcx` -> module if present, else source the
#      caps-self bind-mounted /opt/hpcx tree.
#   2. Node file:  `$PBS_NODEFILE` -> a hostfile generated from the Slurm
#      allocation (`scontrol show hostnames "$SLURM_JOB_NODELIST"`), one line per
#      node with `slots=$DEVICES` so Open MPI's `--map-by ppr:$DEVICES:node`
#      places exactly DEVICES ranks/node (same as PBS).
#
# The per-SKU mpirun invocations (the perf-critical NCCL tuning) are unchanged,
# so benchmark numbers — and therefore the telemetry — stay consistent.
#
# Exit status: the script ends on the `case ${SKU}` whose matched branch's last
# command is `mpirun`, so the JOB exit code == mpirun's exit code (a crashed /
# timed-out run propagates non-zero). That is exactly the signal
# check_slurm_exit_codes() / PR 740 rely on — do NOT append commands after esac.
#
# Env (injected via sbatch --export by queue-nccl-slurm.sh):
#   NUM_NODES SHARP NODES_LIST LOCAL TEST SKU DEVICES BENCHMARK_TARGET

# The in-container slurmd runs Slurm jobs as ROOT; Open MPI refuses to run as root
# without explicit opt-in. (The host-launch path runs as the non-root `capi` user on
# the worker host, so run-nccl-host.sh does not need this.)
export OMPI_ALLOW_RUN_AS_ROOT=1
export OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1

# 1. Load HPC-X (provides mpirun). hpc-image-val2 VMs have Lmod; caps-self slurmd
#    has no module system but bind-mounts /opt/hpcx.
if command -v module >/dev/null 2>&1 && module load mpi/hpcx 2>/dev/null; then
    :
elif [ -r /opt/hpcx/hpcx-init.sh ]; then
    # shellcheck disable=SC1091
    source /opt/hpcx/hpcx-init.sh && hpcx_load
else
    echo "##[warning]HPC-X not found (no 'module load mpi/hpcx', no /opt/hpcx/hpcx-init.sh)"
fi

# 2. Build the Open MPI hostfile from the Slurm allocation (replaces $PBS_NODEFILE).
NODEFILE="${PWD}/nodefile.${SLURM_JOB_ID}"
scontrol show hostnames "${SLURM_JOB_NODELIST}" | sed "s/\$/ slots=${DEVICES}/" > "${NODEFILE}"

SHARP_ARGS=""
if [ $SHARP -eq "1" ]; then
    SHARP_ARGS="-x NCCL_COLLNET_ENABLE=1 -x NCCL_ALGO=CollNet -x SHARP_COLL_ENABLE_SAT=1 \
    -x SHARP_COLL_LOG_LEVEL=3  -x NCCL_DEBUG_SUBSYS=INIT -x SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1 \
    -x SHARP_COLL_NUM_COLL_GROUP_RESOURCE_ALLOC_THRESHOLD=0 -x SHARP_COLL_LOCK_ON_COMM_INIT=1"
fi

MSG_START="8"
MSG_END="16G"

LOCAL_ARGS=""
if [ $LOCAL -eq "1" ]; then
	LOCAL_ARGS="-x NCCL_P2P_DISABLE=1 -x NCCL_SHM_DISABLE=1"
	MSG_START="4G"
	MSG_END="4G"
fi

# Transport-aware NCCL data-path args (mirrors run-nccl-host.sh). NCCL uses its own
# IB verbs (NCCL_IB_HCA) independent of UCX; UCX_TLS selects the mode. Default is IB
# (rc): the in-container slurmd pod has /dev/infiniband + the NCCL IB plugin
# (/usr/local/nccl-rdma-sharp-plugins), so cross-node RDMA works in-container. Set
# UCX_TLS=tcp for the Ethernet fallback. Only the ND40rs_v2 branch consumes these.
UCX_TLS="${UCX_TLS:-rc,sm,self}"
UCX_NET_DEVICES="${UCX_NET_DEVICES:-mlx5_ib0:1}"
if [[ "$UCX_TLS" == *rc* ]]; then
    NCCL_NET_ARGS="-x NCCL_IB_HCA=${NCCL_IB_HCA:-mlx5_ib0} -x NCCL_IB_PCI_RELAXED_ORDERING=1 -x NCCL_SOCKET_IFNAME=eth0"
    echo "# transport: InfiniBand (NCCL_IB_HCA=${NCCL_IB_HCA:-mlx5_ib0}, UCX_TLS=$UCX_TLS)"
else
    NCCL_NET_ARGS="-x NCCL_IB_DISABLE=1 -x NCCL_SOCKET_IFNAME=eth0"
    echo "# transport: TCP (NCCL_IB_DISABLE=1)"
fi

case ${TEST} in
    "allreduce")
        TEST="all_reduce_perf"
    ;;
    "allgather")
        TEST="all_gather_perf"
    ;;
    "alltoall")
        TEST="alltoall_perf"
    ;;
    *)
        echo "Error! Incorrect test provided"
        exit 1
esac

date
echo "# Test: ${TEST}"
echo "# Node count: ${NUM_NODES}"
echo "# Slurm node list: ${SLURM_JOB_NODELIST}"
echo "# PBS-form node list: ${NODES_LIST}"

case ${SKU} in
    "Standard_ND128isr_NDR_GB200_v6"|"Standard_ND128isr_GB300_v6")
        echo "# SKU: ${SKU}"
        echo "# Benchmark target: ${BENCHMARK_TARGET}"
        case ${BENCHMARK_TARGET} in
            "NVLINK")
                mpirun \
                    -np $(( $NUM_NODES * $DEVICES )) \
                    -hostfile ${NODEFILE} \
                    --bind-to none \
                    --map-by ppr:$DEVICES:node \
                    --timeout 1800 \
                    -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
                    -mca coll_hcoll_enable 0 \
                    -x NCCL_COLLNET_ENABLE=1 \
                    -x NCCL_CUMEM_ENABLE=1 \
                    -x NCCL_MNNVL_ENABLE=1 \
                    -x NCCL_SHM_DISABLE=0 \
                    -x NCCL_NET_GDR_C2C=0 \
                    -x SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1 \
                    -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
                    -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
                    -x NCCL_SOCKET_IFNAME=eth0 \
                    -x NCCL_DEBUG=WARN \
                    ${SHARP_ARGS} \
                    ${LOCAL_ARGS} \
                    /opt/nccl-tests/build/${TEST} -b ${MSG_START} -e ${MSG_END} -f 2 -g 1 -c 1 -n 50
            ;;
            "INFINIBAND")
                mpirun \
                    -np $(( $NUM_NODES * $DEVICES )) \
                    -hostfile ${NODEFILE} \
                    --bind-to none \
                    --map-by ppr:$DEVICES:node \
                    --timeout 1800 \
                    -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
                    -mca coll_hcoll_enable 0 \
                    -x NCCL_COLLNET_ENABLE=0 \
                    -x NCCL_CUMEM_ENABLE=0 \
                    -x NCCL_MNNVL_ENABLE=0 \
                    -x NCCL_SHM_DISABLE=1 \
                    -x NCCL_NET_GDR_C2C=1 \
                    -x SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1 \
                    -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
                    -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
                    -x NCCL_SOCKET_IFNAME=eth0 \
                    -x NCCL_DEBUG=WARN \
                    ${SHARP_ARGS} \
                    ${LOCAL_ARGS} \
                    /opt/nccl-tests/build/${TEST} -b ${MSG_START} -e ${MSG_END} -f 2 -g 1 -c 1 -n 50
            ;;
            *)
                echo "Error! Incorrect benchmark target (${BENCHMARK_TARGET}) provided"
                exit 1
        esac
     ;;
    "Standard_ND96isr_H100_v5"|"Standard_ND96isr_H200_v5")
        echo "# SKU: ${SKU}"
        mpirun \
            -np $(( $NUM_NODES * $DEVICES )) \
            -hostfile ${NODEFILE} \
            --bind-to numa \
            --map-by ppr:$DEVICES:node \
            --timeout 1800 \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
            -mca coll_hcoll_enable 0 \
            -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            -x NCCL_DEBUG=WARN \
            ${SHARP_ARGS} \
            ${LOCAL_ARGS} \
            /opt/nccl-tests/build/${TEST} -b ${MSG_START} -e ${MSG_END} -f 2 -g 1 -c 1 -n 50
    ;;
    "Standard_ND96amsr_A100_v4")
        echo "# SKU: ${SKU}"
        mpirun \
            -np $(( $NUM_NODES * $DEVICES )) \
            -hostfile ${NODEFILE} \
            --bind-to numa \
            --map-by ppr:$DEVICES:node \
            --timeout 1800 \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
            -mca coll_hcoll_enable 0 \
            -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            -x NCCL_DEBUG=WARN \
            ${SHARP_ARGS} \
            ${LOCAL_ARGS} \
            -x NCCL_TOPO_FILE=/opt/microsoft/ndv4-topo.xml \
            /opt/nccl-tests/build/${TEST} -b ${MSG_START} -e ${MSG_END} -f 2 -g 1 -c 1 -n 50
    ;;
    "Standard_ND96asr_v4")
        echo "# SKU: ${SKU}"
        mpirun \
            -np $(( $NUM_NODES * $DEVICES )) \
            -hostfile ${NODEFILE} \
            --bind-to numa \
            --map-by ppr:$DEVICES:node \
            --timeout 1800 \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
            -mca coll_hcoll_enable 0 \
            -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            -x NCCL_DEBUG=WARN \
            ${SHARP_ARGS} \
            ${LOCAL_ARGS} \
            -x NCCL_TOPO_FILE=/opt/microsoft/ndv4-topo.xml \
            /opt/nccl-tests/build/${TEST} -b 1K -e 4G -f 2 -g 1 -c 1 -n 50
    ;;
    "Standard_NC24rs_v3")
        echo "# SKU: ${SKU}"
        mpirun \
            -np $(( $NUM_NODES * $DEVICES )) \
            -hostfile ${NODEFILE} \
            --map-by ppr:$DEVICES:node \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
            -mca coll_hcoll_enable 0 \
            -x NCCL_IB_PCI_RELAXED_ORDERING=1 \
            -x UCX_TLS=tcp \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            /opt/nccl-tests/build/${TEST} -b 1K -e 4G -f 2 -g 1 -n 50
	;;
    "Standard_ND40rs_v2")
        # 8x V100 + 100Gb EDR InfiniBand. Transport chosen by UCX_TLS (default
        # rc,sm,self => IB; set UCX_TLS=tcp for the Ethernet fallback). In-container
        # RDMA works on caps-self: the slurmd pod has /dev/infiniband + the NCCL IB
        # plugin. -b 1K -e 4G with -c 1: V100 has 32GB HBM so larger buffers OOM with
        # correctness on. Matches run-nccl-host.sh tuning so telemetry stays consistent.
        echo "# SKU: ${SKU}"
        mpirun \
            -np $(( $NUM_NODES * $DEVICES )) \
            -hostfile ${NODEFILE} \
            --map-by ppr:$DEVICES:node \
            --bind-to numa \
            --timeout 1800 \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
            -mca coll_hcoll_enable 0 \
            -x UCX_TLS="$UCX_TLS" \
            -x UCX_NET_DEVICES="$UCX_NET_DEVICES" \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_DEBUG=WARN \
            ${NCCL_NET_ARGS} ${SHARP_ARGS} ${LOCAL_ARGS} \
            /opt/nccl-tests/build/${TEST} -b 1K -e 4G -f 2 -g 1 -c 1 -n 50
    ;;
    "Standard_NC256lds_xl_RTXPRO6000BSE_v6")
        echo "# SKU: ${SKU}"
        mpirun \
            -np $(( $NUM_NODES * $DEVICES )) \
            -hostfile ${NODEFILE} \
            --map-by ppr:$DEVICES:node \
            --timeout 1800 \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            -x NCCL_DEBUG=WARN \
            ${LOCAL_ARGS} \
            /opt/nccl-tests/build/${TEST} -b 1K -e 4G -f 2 -g 1 -c 1 -n 50
    ;;
    *)
        echo "Error! Incorrect sku (${SKU}) provided"
        exit 1
esac
