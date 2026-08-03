#!/usr/bin/env bash
# =============================================================================
# HPC-X MPI over InfiniBand validation for Standard_ND96asr_v4 (caps-self workers)
# =============================================================================
# Runs OSU micro-benchmarks over the Mellanox HDR InfiniBand fabric via HPC-X/UCX.
# Meant to run ON a worker host (user `capi`); slurm-host-mpi.sh stages it on the
# allocated head node and executes it with stdin detached.
#
# Modes:
#   (default) single-node 2-rank IB *loopback*  -> UCX_TLS=rc,self
#       Shared memory is excluded from the transport list, so the two local ranks
#       talk over the RC InfiniBand transport (looped back through the HCA verbs
#       stack). This proves the RDMA path works end-to-end on the SKU even with a
#       single node (used while Azure has capacity for only one ND96asr_v4).
#
#   HOSTS=10.0.2.4:1,10.0.2.5:1  -> true cross-node run, UCX_TLS=rc,sm,self
#       Requires passwordless SSH host->host and HPC-X present on both (identical
#       ubuntu-hpc image). Use once a 2nd ND96asr_v4 instance allocates.
#
# Tunables (env): HOSTS, UCX_TLS, UCX_NET_DEVICES (default mlx5_ib0:1).
# =============================================================================

echo "=== host: $(hostname) ==="
echo "=== InfiniBand link state ==="
ibstat | grep -E "CA '|State:|Physical state:|Rate:" || echo "(ibstat unavailable)"
echo

# --- locate + load HPC-X -----------------------------------------------------
HPCX_DIR=$(ls -d /opt/hpcx-v2* /opt/hpcx* 2>/dev/null | head -1)
if [ -n "$HPCX_DIR" ] && [ -f "$HPCX_DIR/hpcx-init.sh" ]; then
  echo "HPCX_DIR=$HPCX_DIR"
  # shellcheck disable=SC1090
  source "$HPCX_DIR/hpcx-init.sh"
  hpcx_load
else
  echo "hpcx-init.sh not under /opt/hpcx*; falling back to environment-modules"
  source /etc/profile.d/modules.sh 2>/dev/null || true
  module load mpi/hpcx 2>/dev/null || true
fi
echo "mpirun: $(command -v mpirun || echo MISSING)"
echo "HPCX_OSU_DIR=${HPCX_OSU_DIR:-unset}"

# --- locate OSU benchmark binaries ------------------------------------------
OSU="${HPCX_OSU_DIR:-$HPCX_DIR}"
LAT=$(find "$OSU" /opt/hpcx* -name osu_latency -type f 2>/dev/null | head -1)
BW=$(find "$OSU" /opt/hpcx* -name osu_bw -type f 2>/dev/null | head -1)
echo "osu_latency=$LAT"
echo "osu_bw=$BW"
if [ -z "$LAT" ] || [ -z "$BW" ]; then
  echo "ERROR: could not find OSU benchmarks; aborting." >&2
  exit 1
fi
echo

# --- transport / placement ---------------------------------------------------
DEV="${UCX_NET_DEVICES:-mlx5_ib0:1}"
if [ -n "${HOSTS:-}" ]; then
  NP=$(echo "$HOSTS" | tr ',' '\n' | grep -c .)
  PLACE="--host $HOSTS"
  TLS="${UCX_TLS:-rc,sm,self}"
  echo "### cross-node mode: HOSTS=$HOSTS np=$NP UCX_TLS=$TLS dev=$DEV"
else
  NP=2
  PLACE=""
  TLS="${UCX_TLS:-rc,self}"   # exclude sm so loopback still uses the IB HCA
  echo "### single-node IB loopback: np=$NP UCX_TLS=$TLS dev=$DEV"
fi
COMMON="--allow-run-as-root -np $NP $PLACE --bind-to core \
  -x LD_LIBRARY_PATH -x UCX_NET_DEVICES=$DEV -x UCX_TLS=$TLS"

# Keep mpirun detached from caller stdin so it cannot interfere with automation.
echo
echo "=== osu_latency (UCX_TLS=$TLS) ==="
mpirun $COMMON "$LAT" </dev/null
echo
echo "=== osu_bw (UCX_TLS=$TLS) ==="
mpirun $COMMON "$BW" </dev/null
echo
echo "=== transport selection (UCX_LOG_LEVEL=info) ==="
mpirun $COMMON -x UCX_LOG_LEVEL=info "$LAT" </dev/null 2>&1 \
  | grep -iE "mlx5_ib0|rc_mlx5|rc_verbs|selected|using transport|ib/mlx5" | head -15 \
  || echo "(no UCX transport lines captured)"
