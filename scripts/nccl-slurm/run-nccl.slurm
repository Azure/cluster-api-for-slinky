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
        echo "# SKU: ${SKU}"
        mpirun \
            -np $(( $NUM_NODES * $DEVICES )) \
            -hostfile ${NODEFILE} \
            --map-by ppr:$DEVICES:node \
            -x LD_LIBRARY_PATH=/usr/local/nccl-rdma-sharp-plugins/lib:$LD_LIBRARY_PATH \
            -mca coll_hcoll_enable 0 \
            -x UCX_TLS=tcp \
            -x CUDA_DEVICE_ORDER=PCI_BUS_ID \
            -x NCCL_SOCKET_IFNAME=eth0 \
            /opt/nccl-tests/build/${TEST} -b 1K -e 4G -f 2 -g 1 -n 50
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
