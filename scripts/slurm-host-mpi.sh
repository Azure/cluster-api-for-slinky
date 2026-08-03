#!/usr/bin/env bash
#
# slurm-host-mpi.sh - "node self-bootstrap" MPI launch on caps-self.
#
# Slurm ALLOCATES the nodes; the MPI job is then launched from the HOST namespace
# (directly on the worker VM over SSH), NOT from inside the slurmd container. This
# is the alternative to the in-container `srun --mpi=pmix` path documented in
# docs/selfmanaged-hpc-stack.md §8.
#
# Rationale: running mpirun on the host gives HPC-X / UCX direct, native access to
# the host NICs and /dev/infiniband (no container-namespace indirection), which is
# the cleaner path for InfiniBand / RDMA. Slurm is used purely as the resource
# allocator (which nodes, held exclusively), mirroring the team's PBS-over-SSH flow
# in hpc-image-val2 ("scheduler allocates, mpirun runs over SSH").
#
# Flow:
#   1. salloc --no-shell -N$NODES     Slurm reserves the nodes exclusively (ALLOCATE).
#   2. Resolve allocated HOST IPs       use `hostname -I` for host-networked
#                                      slurmd; otherwise map Slurm NodeAddr
#                                      (pod IP) -> K8s node -> InternalIP.
#   3. ssh capi@<head-host>            stage and run the launcher on the HOST,
#                                      then mpirun --host <ip:slots,...>,
#                                      loading HPC-X from the host image. Delegated
#                                      to scripts/ib-osu-loopback.sh (HOSTS=... mode).
#   4. scancel                         release the allocation.
#
# Runs ON the CAPZ mgmt VM (same context as scripts/demo-presentation.sh): it has
# kubectl to the workload cluster (via the caps-self-kubeconfig secret) AND SSH to
# the worker hosts (capi@<priv-ip>). Invoke from the workstation with:
#   bash scripts/azure-remote.sh ssh 'cd "$REMOTE_REPO" && bash scripts/slurm-host-mpi.sh'
#
# Env overrides:
#   NODES=2                    nodes to allocate (>=2 for a cross-node run)
#   NTASKS_PER_NODE=1          MPI ranks per node (slots in the --host string)
#   UCX_TLS=tcp                transport: tcp (Ethernet) | rc,sm,self (InfiniBand)
#   UCX_NET_DEVICES=eth0       NIC/HCA: eth0 (TCP) | mlx5_ib0:1 (InfiniBand)
#   WORKER_SSH_USER=capi       worker host SSH user
#   WORKER_SSH_KEY=~/.ssh/caps-workload   private key for worker host SSH (this key's
#                              pubkey is in every worker's authorized_keys via the
#                              manifest sshPublicKey)
#   SETUP_SSH=1                bootstrap passwordless host->host SSH for mpirun's
#                              remote orted launch (copies WORKER_SSH_KEY onto the
#                              head host; removed again on exit). Set 0 to skip.
#   REMOTE_ARTIFACT_PATH        optional path produced by the launcher on HEAD.
#   LOCAL_ARTIFACT_DIR          copy REMOTE_ARTIFACT_PATH here after a successful
#                              launch (required when REMOTE_ARTIFACT_PATH is set).
#   CLEAN_REMOTE_ARTIFACT=1     remove the remote artifact after a successful copy.
#   SLURM_NS=slurm             namespace of the slurm pods
#   KC=/tmp/caps-self.kubeconfig  workload kubeconfig path (auto-fetched if absent)
#
# To flip to InfiniBand / RDMA: UCX_TLS=rc,sm,self UCX_NET_DEVICES=mlx5_ib0:1 ...
set -euo pipefail

NODES="${NODES:-2}"
NTASKS_PER_NODE="${NTASKS_PER_NODE:-1}"
UCX_TLS="${UCX_TLS:-tcp}"
UCX_NET_DEVICES="${UCX_NET_DEVICES:-eth0}"
WORKER_SSH_USER="${WORKER_SSH_USER:-capi}"
WORKER_SSH_KEY="${WORKER_SSH_KEY:-$HOME/.ssh/caps-workload}"
SETUP_SSH="${SETUP_SSH:-1}"
SLURM_NS="${SLURM_NS:-slurm}"
KC="${KC:-/tmp/caps-self.kubeconfig}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Host-side launcher script fed to the worker over SSH (default = OSU micro-bench).
# Override to run a different host-namespace benchmark, e.g. the NCCL launcher
# scripts/nccl-slurm/run-nccl-host.sh.
LAUNCHER="${LAUNCHER:-$SCRIPT_DIR/ib-osu-loopback.sh}"
# Extra `KEY=val` env pairs (space-separated) appended to the remote launcher
# command — how the NCCL orchestrator passes TEST/DEVICES/SKU/etc. to run-nccl-host.sh.
LAUNCH_EXTRA_ENV="${LAUNCH_EXTRA_ENV:-}"
# If set, tee the launcher's combined stdout+stderr to this file (a `%j` token is
# replaced with the Slurm allocation job id). Lets the caller preserve the PBS
# `<jobname>.o<jobid>` raw-log convention so benchmark ingestion is unchanged.
OUT_FILE="${OUT_FILE:-}"
REMOTE_ARTIFACT_PATH="${REMOTE_ARTIFACT_PATH:-}"
LOCAL_ARTIFACT_DIR="${LOCAL_ARTIFACT_DIR:-}"
CLEAN_REMOTE_ARTIFACT="${CLEAN_REMOTE_ARTIFACT:-1}"

SSH_OPTS=(-i "$WORKER_SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

# Where the head-host->peer key is staged (a UNIQUE name so it never clobbers an
# existing default key), and the matching ssh options mpirun passes to its rsh
# launcher. Using plm_rsh_args avoids writing ~/.ssh/config on the worker, so the
# bootstrap leaves no residue beyond this single key file (removed on exit).
REMOTE_KEY=".ssh/caps-hostmpi-id"
REMOTE_LAUNCHER=""
RSH_ARGS="-i /home/$WORKER_SSH_USER/$REMOTE_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

log()  { printf '>> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$LAUNCHER" ]] || die "host launcher not found: $LAUNCHER"

# --- workload kubeconfig (mgmt cluster holds the caps-self-kubeconfig secret) ---
if [[ ! -s "$KC" ]]; then
  log "fetching workload kubeconfig -> $KC"
  kubectl get secret caps-self-kubeconfig -n default -o jsonpath='{.data.value}' \
    | base64 -d > "$KC"
fi
kc() { kubectl --kubeconfig "$KC" "$@"; }

# --- pick a slurmd pod to drive the Slurm client commands -----------------------
POD="$(kc get pods -n "$SLURM_NS" -l app.kubernetes.io/name=slurmd \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
[[ -n "$POD" ]] || die "no slurmd pod found in namespace '$SLURM_NS'"
sexec() { kc exec -n "$SLURM_NS" "$POD" -- bash -lc "$1"; }

# --- 1. ALLOCATE: Slurm reserves the nodes (and only allocates) -----------------
log "allocating $NODES node(s) via Slurm (salloc --no-shell, exclusive)"
ALLOC_OUT="$(sexec "salloc -N$NODES --ntasks-per-node=$NTASKS_PER_NODE --exclusive --no-shell -J hostmpi 2>&1" || true)"
JOBID=""
if [[ "$ALLOC_OUT" =~ Granted\ job\ allocation\ ([0-9]+) ]]; then
  JOBID="${BASH_REMATCH[1]}"
fi
[[ -n "$JOBID" ]] || die "salloc did not grant an allocation:\n$ALLOC_OUT"
REMOTE_LAUNCHER=".caps-host-launch-${JOBID}.sh"
log "Slurm job allocation: $JOBID"

# Artifact paths may use the same Slurm-style %j token as OUT_FILE.
REMOTE_ARTIFACT_PATH="${REMOTE_ARTIFACT_PATH//%j/$JOBID}"

cleanup() {
  if [[ -n "${JOBID:-}" ]]; then
    log "releasing Slurm allocation $JOBID (scancel)"
    sexec "scancel $JOBID" 2>/dev/null || true
  fi
  if [[ -n "${HEAD:-}" ]]; then
    ssh "${SSH_OPTS[@]}" "$WORKER_SSH_USER@$HEAD" \
      "rm -f ~/$REMOTE_KEY ~/$REMOTE_LAUNCHER" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# wait for the allocation to be RUNNING before attaching a discovery step
for _ in $(seq 1 30); do
  st="$(sexec "squeue -h -j $JOBID -o %T" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$st" == "RUNNING" ]] && break
  sleep 1
done
[[ "${st:-}" == "RUNNING" ]] || die "allocation $JOBID not RUNNING (state='${st:-unknown}')"

# --- 2. DISCOVER the allocated HOST IPs -----------------------------------------
log "discovering host IPs for the allocation"
IPS_RAW="$(sexec "srun --jobid=$JOBID -N$NODES --ntasks-per-node=1 hostname -I 2>/dev/null" || true)"
# Host-networked slurmd returns the worker's 10.x address directly. Keep this
# fast path for the original caps-self deployment.
mapfile -t HOST_IPS < <(tr ' ' '\n' <<<"$IPS_RAW" | grep -E '^10\.' | awk '!seen[$0]++')

# Pulumi-managed Slinky uses overlay-networked slurmd pods. In that topology,
# `hostname -I` and Slurm NodeAddr are pod addresses (for example 192.168.x.x),
# not SSH-reachable worker addresses. Resolve each allocated Slurm node through
# the pod IP to its Kubernetes node, then read that node's InternalIP.
if [[ "${#HOST_IPS[@]}" -lt "$NODES" ]]; then
  mapfile -t SLURM_NODES < <(
    sexec "srun --jobid=$JOBID -N$NODES --ntasks-per-node=1 hostname 2>/dev/null" \
      | awk 'NF && !seen[$0]++'
  )
  HOST_IPS=()
  for slurm_node in "${SLURM_NODES[@]}"; do
    node_addr="$(
      sexec "scontrol show node '$slurm_node' -o" \
        | sed -n 's/.* NodeAddr=\([^ ]*\).*/\1/p'
    )"
    [[ -n "$node_addr" ]] || continue

    if [[ "$node_addr" == 10.* ]]; then
      host_ip="$node_addr"
    else
      k8s_node="$(
        kc get pods -n "$SLURM_NS" -l app.kubernetes.io/name=slurmd \
          -o "jsonpath={range .items[?(@.status.podIP=='$node_addr')]}{.spec.nodeName}{'\n'}{end}" \
          2>/dev/null
      )"
      [[ -n "$k8s_node" ]] || continue
      host_ip="$(
        kc get node "$k8s_node" \
          -o "jsonpath={range .status.addresses[?(@.type=='InternalIP')]}{.address}{'\n'}{end}" \
          2>/dev/null
      )"
    fi

    [[ -n "$host_ip" ]] || continue
    host_ip_seen=0
    for existing_ip in "${HOST_IPS[@]}"; do
      [[ "$existing_ip" == "$host_ip" ]] && host_ip_seen=1 && break
    done
    [[ "$host_ip_seen" == "1" ]] || HOST_IPS+=("$host_ip")
  done
fi
[[ "${#HOST_IPS[@]}" -ge "$NODES" ]] || \
  die "discovered ${#HOST_IPS[@]} host IP(s), expected $NODES (hostname -I: $IPS_RAW)"
HEAD="${HOST_IPS[0]}"
log "allocated host IPs: ${HOST_IPS[*]}  (head: $HEAD)"

# --host ip:slots,ip:slots,...
HOSTS=""
for ip in "${HOST_IPS[@]}"; do HOSTS+="${ip}:${NTASKS_PER_NODE},"; done
HOSTS="${HOSTS%,}"

# --- bootstrap passwordless host->host SSH for mpirun's remote launch -----------
# mpirun on the head host needs to SSH the other hosts to spawn orted. The same
# key that authorizes us (WORKER_SSH_KEY, in every worker's authorized_keys via the
# manifest sshPublicKey) is placed on the head host so it can reach the peers.
# Production "node self-bootstrap" would bake this into the image / cloud-init.
if [[ "$SETUP_SSH" == "1" && "$NODES" -gt 1 ]]; then
  log "bootstrapping host->host SSH on $HEAD (single key file, no ~/.ssh/config changes)"
  scp "${SSH_OPTS[@]}" "$WORKER_SSH_KEY" "$WORKER_SSH_USER@$HEAD:$REMOTE_KEY" >/dev/null
  ssh "${SSH_OPTS[@]}" "$WORKER_SSH_USER@$HEAD" "chmod 600 ~/$REMOTE_KEY"
  SSH_KEY_PUSHED=1
fi

# --- 3. LAUNCH from the HOST namespace: mpirun runs on the worker VM, not the pod -
log "launching on host $HEAD via $(basename "$LAUNCHER") (HOSTS=$HOSTS, UCX_TLS=$UCX_TLS, dev=$UCX_NET_DEVICES)"
scp "${SSH_OPTS[@]}" "$LAUNCHER" "$WORKER_SSH_USER@$HEAD:$REMOTE_LAUNCHER" >/dev/null
ssh "${SSH_OPTS[@]}" "$WORKER_SSH_USER@$HEAD" "chmod 700 ~/$REMOTE_LAUNCHER"
# Tell OpenMPI's rsh launcher which key + ssh opts to use for host->host orted
# spawn (only when we staged a key), so we never have to touch ~/.ssh/config.
LAUNCH_ENV="HOSTS='$HOSTS' UCX_TLS='$UCX_TLS' UCX_NET_DEVICES='$UCX_NET_DEVICES'"
LAUNCH_ENV="HOST_LAUNCH_JOB_ID='$JOBID' $LAUNCH_ENV"
if [[ "${SSH_KEY_PUSHED:-0}" == "1" ]]; then
  LAUNCH_ENV="OMPI_MCA_plm_rsh_args='$RSH_ARGS' $LAUNCH_ENV"
fi
# Extra caller-supplied env (e.g. NCCL TEST/DEVICES/SKU) appended verbatim.
[[ -n "$LAUNCH_EXTRA_ENV" ]] && LAUNCH_ENV="$LAUNCH_ENV $LAUNCH_EXTRA_ENV"

# The values are intentionally expanded locally and baked into the remote command
# so the host-side launcher sees them as env. Capture the REMOTE exit code (== the
# launcher's, i.e. mpirun's when the launcher ends on mpirun) so a failed benchmark
# propagates as this script's exit code — the host-launch analog of PR 740's
# scheduler-exit-code gate (the Slurm allocation itself can't carry it here).
# shellcheck disable=SC2029
if [[ -n "$OUT_FILE" ]]; then
  OUT_FILE="${OUT_FILE//%j/$JOBID}"
  mkdir -p "$(dirname "$OUT_FILE")"
  ssh "${SSH_OPTS[@]}" "$WORKER_SSH_USER@$HEAD" \
    "$LAUNCH_ENV bash /home/$WORKER_SSH_USER/$REMOTE_LAUNCHER" \
    </dev/null 2>&1 | tee "$OUT_FILE"
  LAUNCH_RC=${PIPESTATUS[0]}
  log "raw log written to $OUT_FILE"
else
  ssh "${SSH_OPTS[@]}" "$WORKER_SSH_USER@$HEAD" \
    "$LAUNCH_ENV bash /home/$WORKER_SSH_USER/$REMOTE_LAUNCHER" </dev/null
  LAUNCH_RC=$?
fi

if [[ "$LAUNCH_RC" -eq 0 && -n "$REMOTE_ARTIFACT_PATH" ]]; then
  [[ -n "$LOCAL_ARTIFACT_DIR" ]] \
    || die "LOCAL_ARTIFACT_DIR is required when REMOTE_ARTIFACT_PATH is set"
  mkdir -p "$LOCAL_ARTIFACT_DIR"
  log "copying remote artifact $HEAD:$REMOTE_ARTIFACT_PATH -> $LOCAL_ARTIFACT_DIR"
  scp -r "${SSH_OPTS[@]}" \
    "$WORKER_SSH_USER@$HEAD:$REMOTE_ARTIFACT_PATH" "$LOCAL_ARTIFACT_DIR/"
  if [[ "$CLEAN_REMOTE_ARTIFACT" == "1" ]]; then
    if ! ssh "${SSH_OPTS[@]}" "$WORKER_SSH_USER@$HEAD" \
      "rm -rf -- '$REMOTE_ARTIFACT_PATH'"; then
      log "WARNING: copied artifact but could not remove remote path $REMOTE_ARTIFACT_PATH"
    fi
  fi
fi

log "launcher exit code: $LAUNCH_RC (allocation $JOBID released on exit)"
exit "$LAUNCH_RC"
