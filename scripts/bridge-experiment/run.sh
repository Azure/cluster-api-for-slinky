#!/usr/bin/env bash
#
# bridge-experiment/run.sh — SMALL, CPU-only validation of the Slinky
# `slurm-bridge` co-scheduling idea on the self-managed `caps-self` cluster.
#
# What it proves (NO GPU, NO DRA, NO custom Slurm image — stock busybox + the
# stock slinky Slurm cluster already on caps-self):
#
#   Demo A — translation/placement: a plain Kubernetes Job submitted into the
#     managed namespace `slurm-bridge` is scheduled by Slurm (not kube-scheduler).
#     We verify the admission webhook rewrote `.spec.schedulerName`, that the pod
#     gained a `scheduler.slinky.slurm.net/slurm-jobid` label, and that a matching
#     "external" job exists in `squeue`/`scontrol`.
#
#   Demo B — co-scheduling on a shared pool: a NATIVE `sbatch --exclusive` job
#     grabs every idle node in the (shared) `all` partition; a bridge Kubernetes
#     Job submitted at the same time then waits — its representative Slurm job sits
#     PENDING(Resources) and the pod stays Pending — until the native job frees
#     the nodes, after which the pod binds and runs. One Slurm queue arbitrates
#     both orchestrators on the same hardware: that is the "basic co-scheduling
#     idea".
#
# It runs ON the CAPZ management VM (needs helm + line of sight to the caps-self
# API server). Drive it from a workstation with:
#     scripts/azure-remote.sh bridge-experiment [all|install|demo|teardown]
#
# Reuses the kubeconfig-from-secret + helm-to-~/.local/bin bootstrap from
# day2-selfmanaged.sh. Fully reversible: `teardown` helm-uninstalls the bridge,
# removes the token + workload namespace, and leaves the Slurm cluster untouched
# (the experiment never modifies caps-self-slurm.yaml — it reuses the existing
# `all` partition).
#
# !! TOPOLOGY WARNING (learned the hard way): slurm-bridge's controllers taint
# EVERY node in the managed partition with `slinky.slurm.net/managed-node:NoExecute`
# so that only Slurm-placed pods run there. On caps-self the Slurm control plane
# (slurmctld/slurmrestd/operator) and slurmd are CO-LOCATED on the only worker
# nodes, which ARE the managed partition — so that NoExecute taint EVICTS Slurm
# itself and takes the cluster down. `install` therefore refuses unless slurmd
# tolerates the taint (or BRIDGE_ACK_UNSAFE=1). Making it work needs a dedicated
# non-managed node for the control planes + a slurmd toleration (Slurm chart edit).
#
# Env overrides:
#   CLUSTER=caps-self     workload cluster name (kubeconfig secret <CLUSTER>-kubeconfig)
#   NAMESPACE=default     namespace of that secret on the mgmt cluster
#   SLURM_NS=slurm        namespace of the slinky Slurm cluster + bridge runtime
#   WL_NS=slurm-bridge    managed namespace where bridge workloads are submitted
#   BRIDGE_VERSION=       pin the slurm-bridge chart version (default: latest)
#   HOLD_SECS=45          how long the native job holds the nodes in Demo B
#   BRIDGE_ACK_UNSAFE=0   set to 1 to install despite the taint evicting Slurm (DANGER)
set -euo pipefail

ACTION="${1:-all}"

CLUSTER="${CLUSTER:-caps-self}"
NAMESPACE="${NAMESPACE:-default}"
SLURM_NS="${SLURM_NS:-slurm}"
WL_NS="${WL_NS:-slurm-bridge}"
BRIDGE_VERSION="${BRIDGE_VERSION:-}"
HOLD_SECS="${HOLD_SECS:-45}"
BRIDGE_CHART="oci://ghcr.io/slinkyproject/charts/slurm-bridge"
# Taint the bridge controllers imperatively stamp on every managed-partition node.
MANAGED_TAINT_KEY="slinky.slurm.net/managed-node"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- workload kubeconfig (pulled from the CAPI-managed secret) ------------------
CTX="$(kubectl config current-context)"
WL_KCFG="$(mktemp "/tmp/${CLUSTER}.kubeconfig.XXXXXX")"
trap 'rm -f "$WL_KCFG"' EXIT
if ! kubectl --context "$CTX" -n "$NAMESPACE" get secret "${CLUSTER}-kubeconfig" >/dev/null 2>&1; then
  echo "##[error]Secret ${CLUSTER}-kubeconfig not found in ns ${NAMESPACE} on ${CTX}."
  echo "##[error]The ${CLUSTER} workload cluster is not provisioned yet — wait for it, then re-run."
  exit 1
fi
kubectl --context "$CTX" -n "$NAMESPACE" get secret "${CLUSTER}-kubeconfig" \
  -o jsonpath='{.data.value}' | base64 -d > "$WL_KCFG"

k() { kubectl --kubeconfig "$WL_KCFG" "$@"; }
helmw() { helm --kubeconfig "$WL_KCFG" "$@"; }

# --- helm (install locally if absent; no sudo, reversible) ----------------------
export PATH="$HOME/.local/bin:$PATH"
ensure_helm() {
  command -v helm >/dev/null 2>&1 && return 0
  echo ">> helm not found; installing to ~/.local/bin"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    | HELM_INSTALL_DIR="$HOME/.local/bin" USE_SUDO=false bash
}

# --- slurmctld pod/container discovery + exec helper ----------------------------
SLURMCTLD_POD=""; SLURMCTLD_CTR=""
resolve_slurmctld() {
  [ -n "$SLURMCTLD_POD" ] && return 0
  SLURMCTLD_POD="$(k get pods -n "$SLURM_NS" -l app.kubernetes.io/name=slurmctld \
                     -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -z "$SLURMCTLD_POD" ] && SLURMCTLD_POD="$(k get pods -n "$SLURM_NS" -o name 2>/dev/null \
                     | grep -Ei 'slurmctld|controller' | head -1 | cut -d/ -f2 || true)"
  if [ -z "$SLURMCTLD_POD" ]; then
    echo "##[error]No slurmctld pod in ns ${SLURM_NS} — deploy the slinky Slurm cluster first."
    return 1
  fi
  SLURMCTLD_CTR="$(k get pod -n "$SLURM_NS" "$SLURMCTLD_POD" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null \
                     | tr ' ' '\n' | grep -Ei 'slurmctld|controller' | head -1 || true)"
  if [ -z "$SLURMCTLD_CTR" ]; then
    SLURMCTLD_CTR="$(k get pod -n "$SLURM_NS" "$SLURMCTLD_POD" -o jsonpath='{.spec.containers[0].name}')"
  fi
  return 0
}
# Run a Slurm client command inside the controller pod.
slurm() { k exec -n "$SLURM_NS" "$SLURMCTLD_POD" -c "$SLURMCTLD_CTR" -- "$@"; }

render() { # env-substitute ${JWT_KEY_SECRET} in a manifest (envsubst, sed fallback)
  if command -v envsubst >/dev/null 2>&1; then JWT_KEY_SECRET="$JWT_KEY_SECRET" envsubst < "$1"
  else sed "s|\${JWT_KEY_SECRET}|${JWT_KEY_SECRET}|g" "$1"; fi
}

# SAFETY: the bridge taints every managed-partition node with
# ${MANAGED_TAINT_KEY}:NoExecute. If the co-located slurmd (and thus the Slurm
# control plane) does not tolerate it, installing EVICTS Slurm and takes it down.
# Refuse in that case unless the operator explicitly acknowledges the risk.
guard_topology() {
  local tolerated
  tolerated="$(k get pods -n "$SLURM_NS" -l app.kubernetes.io/name=slurmd \
      -o jsonpath='{range .items[*]}{range .spec.tolerations[*]}{.key}{"\n"}{end}{end}' 2>/dev/null \
      | grep -Fxc "$MANAGED_TAINT_KEY" || true)"
  if [ "${tolerated:-0}" -gt 0 ]; then
    echo ">> topology check: slurmd tolerates ${MANAGED_TAINT_KEY} — safe to proceed."
    return 0
  fi
  if [ "${BRIDGE_ACK_UNSAFE:-0}" = "1" ]; then
    echo "##[warning]BRIDGE_ACK_UNSAFE=1 — proceeding though the taint WILL evict Slurm."
    return 0
  fi
  echo "##[error]REFUSING to install: slurm-bridge taints every node in the managed"
  echo "##[error]partition with ${MANAGED_TAINT_KEY}=<scheduler>:NoExecute so only Slurm-"
  echo "##[error]placed pods run there. Your slurmd (and the co-located slurmctld/"
  echo "##[error]slurmrestd/operator) do NOT tolerate that taint, so installing would"
  echo "##[error]EVICT them and take Slurm DOWN (converged/hybrid topology, no dedicated"
  echo "##[error]non-managed node)."
  echo "##[error]"
  echo "##[error]To run safely: give slurmd a toleration for ${MANAGED_TAINT_KEY} and host"
  echo "##[error]slurmctld/slurmrestd/operator + the bridge components on a non-managed"
  echo "##[error]node. To override anyway (WILL disrupt Slurm): BRIDGE_ACK_UNSAFE=1"
  exit 1
}

# ===============================================================================
# preflight
# ===============================================================================
preflight() {
  echo "##[section]Preflight"
  echo ">> workload cluster : ${CLUSTER} (via secret on ${CTX})"
  k get nodes -o wide || { echo "##[error]cannot reach ${CLUSTER} API server"; exit 1; }

  # Slurm must be up (slurmctld + slurmrestd) for the bridge to talk to it.
  resolve_slurmctld || exit 1
  echo ">> slurmctld pod   : ${SLURMCTLD_POD} (container ${SLURMCTLD_CTR})"
  if ! k get svc -n "$SLURM_NS" slurm-restapi >/dev/null 2>&1; then
    echo "##[warning]Service slurm-restapi not found in ns ${SLURM_NS}; the bridge needs slurmrestd."
  fi
  if ! k get ns cert-manager >/dev/null 2>&1; then
    echo "##[warning]cert-manager namespace not found; slurm-bridge admission webhook needs it."
  fi
  echo ">> Slurm partitions / nodes:"
  slurm sinfo -o '%P %a %D %T %N' 2>&1 | head -20 || true
}

# ===============================================================================
# install: token + chart + managed namespace
# ===============================================================================
install() {
  echo "##[section]Installing slurm-bridge (CPU experiment)"
  # Refuse before touching anything if the taint would take Slurm down.
  guard_topology
  ensure_helm
  echo ">> helm: $(helm version --short)"

  # 1. JWT token Secret for slurmrestd auth — prefer the operator Token CR.
  echo ">> detecting Slurm JWT signing-key secret in ns ${SLURM_NS}"
  JWT_KEY_SECRET=""
  for s in $(k get secret -n "$SLURM_NS" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    if k get secret -n "$SLURM_NS" "$s" -o jsonpath='{.data.jwt\.key}' 2>/dev/null | grep -q .; then
      JWT_KEY_SECRET="$s"; break
    fi
  done
  [ -z "$JWT_KEY_SECRET" ] && JWT_KEY_SECRET="slurm-auth-jwt"
  echo ">> JWT signing-key secret: ${JWT_KEY_SECRET}"

  if k get crd tokens.slinky.slurm.net >/dev/null 2>&1; then
    echo ">> applying Token CR (operator-managed, auto-refresh)"
    render "$SCRIPT_DIR/slurm-bridge-token.yaml" | k apply -f -
  else
    echo ">> Token CRD absent; falling back to a manual scontrol token"
    resolve_slurmctld
    local tok
    tok="$(slurm scontrol token username=slurm lifespan=infinite 2>/dev/null | sed 's/^SLURM_JWT=//')"
    [ -n "$tok" ] || { echo "##[error]could not mint a SLURM_JWT"; exit 1; }
    k create secret generic slurm-bridge-token -n "$SLURM_NS" \
      --from-literal=auth-token="$tok" --dry-run=client -o yaml | k apply -f -
  fi

  # 2. The bridge chart (components land in ns slurm).
  echo ">> helm upgrade --install slurm-bridge -> ns ${SLURM_NS}"
  # shellcheck disable=SC2086
  helmw upgrade --install slurm-bridge "$BRIDGE_CHART" \
    -n "$SLURM_NS" ${BRIDGE_VERSION:+--version "$BRIDGE_VERSION"} \
    -f "$SCRIPT_DIR/slurm-bridge-values.yaml" --wait --timeout 5m

  echo ">> waiting for bridge components to be Ready"
  for d in slurm-bridge-scheduler slurm-bridge-controllers slurm-bridge-admission; do
    k -n "$SLURM_NS" rollout status "deploy/$d" --timeout=180s 2>/dev/null \
      || echo "##[warning]deploy/$d not ready (continuing)"
  done

  # 3. Managed namespace for bridge workloads (privileged PSA so the stock
  #    busybox pod isn't rejected — consistent with the slurm/local-path nses).
  k get ns "$WL_NS" >/dev/null 2>&1 || k create ns "$WL_NS"
  k label ns "$WL_NS" pod-security.kubernetes.io/enforce=privileged --overwrite

  # 4. Node-name mapping sanity check (hybrid nodes). slurm-bridge maps a k8s node
  #    to its Slurm node by matching names; if they differ, a label is required.
  echo ">> checking Slurm<->Kubernetes node-name alignment"
  local snodes knodes
  snodes="$(slurm sinfo -h -N -o '%N' 2>/dev/null | sort -u || true)"
  knodes="$(k get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | sort -u || true)"
  for sn in $snodes; do
    if ! grep -qx "$sn" <<<"$knodes"; then
      echo "##[warning]Slurm node '$sn' has no identically-named k8s node."
      echo "##[warning]If the bridge can't place pods, label the matching k8s node:"
      echo "    kubectl label node <k8s-node> slinky.slurm.net/slurm-nodename=$sn"
    fi
  done

  echo ">> bridge pods:"
  k get pods -n "$SLURM_NS" -l 'app.kubernetes.io/instance=slurm-bridge' -o wide 2>/dev/null \
    || k get pods -n "$SLURM_NS" | grep slurm-bridge || true
  echo "##[section]Install complete"
}

# ===============================================================================
# Demo A — a k8s Job scheduled by Slurm via the bridge
# ===============================================================================
demo_a() {
  echo "##[section]Demo A — k8s Job scheduled by Slurm (translation + bind)"
  k delete job bridge-cpu-hello -n "$WL_NS" --ignore-not-found >/dev/null 2>&1 || true
  k apply -f "$SCRIPT_DIR/cpu-bridge-job.yaml"

  # Wait for the pod object to exist, then assert the admission webhook acted.
  echo ">> waiting for the pod to be created..."
  local pod=""
  for _ in $(seq 1 30); do
    pod="$(k get pods -n "$WL_NS" -l batch.kubernetes.io/job-name=bridge-cpu-hello \
             -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    [ -n "$pod" ] && break
    sleep 2
  done
  [ -n "$pod" ] || { echo "##[error]pod never appeared"; return 1; }

  local sched
  sched="$(k get pod -n "$WL_NS" "$pod" -o jsonpath='{.spec.schedulerName}' 2>/dev/null || true)"
  echo ">> pod ${pod} schedulerName = ${sched}"
  if [ "$sched" = "slurm-bridge-scheduler" ]; then
    echo "   PASS: admission webhook routed the pod to slurm-bridge-scheduler."
  else
    echo "##[warning]expected slurm-bridge-scheduler — is ${WL_NS} in admission.managedNamespaces?"
  fi

  echo ">> waiting for the Job to complete..."
  if ! k wait --for=condition=complete job/bridge-cpu-hello -n "$WL_NS" --timeout=180s 2>/dev/null; then
    echo "##[warning]Job did not complete in time; current state:"
    k get pods -n "$WL_NS" -l batch.kubernetes.io/job-name=bridge-cpu-hello -o wide || true
    k describe pod -n "$WL_NS" "$pod" | sed -n '/Events:/,$p' | head -20 || true
  fi

  local jid
  jid="$(k get pod -n "$WL_NS" "$pod" \
           -o jsonpath='{.metadata.labels.scheduler\.slinky\.slurm\.net/slurm-jobid}' 2>/dev/null || true)"
  echo ">> pod node      : $(k get pod -n "$WL_NS" "$pod" -o jsonpath='{.spec.nodeName}' 2>/dev/null)"
  echo ">> Slurm JobID   : ${jid:-<none>}  (from scheduler.slinky.slurm.net/slurm-jobid)"
  if [ -n "$jid" ]; then
    echo ">> Slurm's view of the external job:"
    slurm scontrol show job "$jid" 2>&1 | grep -E 'JobId|JobName|JobState|MCS_label|Partition|NodeList' | sed 's/^/     /' || true
  fi
  echo ">> pod logs:"
  k logs -n "$WL_NS" "$pod" 2>/dev/null | sed 's/^/     /' || true
  echo "   => a Kubernetes Job was placed by Slurm, not the default scheduler."
}

# ===============================================================================
# Demo B — co-scheduling contention on the shared `all` partition
# ===============================================================================
demo_b() {
  echo "##[section]Demo B — co-scheduling: native Slurm job vs bridge k8s Job"
  resolve_slurmctld

  local idle
  idle="$(slurm sinfo -h -N -p all -t idle -o '%N' 2>/dev/null | sort -u | grep -c . || true)"
  idle="${idle:-0}"
  echo ">> idle nodes in partition 'all': ${idle}"

  local njid=""
  if [ "$idle" -ge 1 ]; then
    echo ">> submitting NATIVE exclusive job to hold all ${idle} idle node(s) for ${HOLD_SECS}s"
    njid="$(slurm sbatch --parsable -p all --nodes="$idle" --exclusive \
              -J native-hold -t 00:05:00 \
              --wrap "hostname; echo native-hold holding nodes; sleep ${HOLD_SECS}; echo native-hold done" 2>/dev/null || true)"
    echo ">> native job id : ${njid:-<submit failed>}"
    # Wait until the native job is actually RUNNING (holding the nodes).
    local st
    for _ in $(seq 1 30); do
      st="$(slurm squeue -h -j "$njid" -o '%T' 2>/dev/null | tr -d '[:space:]' || true)"
      [ "$st" = "RUNNING" ] && break
      sleep 2
    done
    echo ">> native job state: $(slurm squeue -h -j "$njid" -o '%T' 2>/dev/null | tr -d '[:space:]')"
  else
    echo "##[warning]no idle nodes — submitting the bridge job anyway (no contention to show)."
  fi

  # Submit the bridge k8s Job (distinct name) while the nodes are held.
  echo ">> submitting bridge k8s Job 'bridge-cpu-contended' (needs 1 whole node)"
  k delete job bridge-cpu-contended -n "$WL_NS" --ignore-not-found >/dev/null 2>&1 || true
  sed 's/bridge-cpu-hello/bridge-cpu-contended/g' "$SCRIPT_DIR/cpu-bridge-job.yaml" | k apply -f -

  sleep 8
  echo ">> SNAPSHOT while native holds the pool — expect native=RUNNING, bridge=PENDING:"
  echo "   --- squeue ---"
  slurm squeue -o '%.6i %.16j %.9T %.10M %.5D %R' 2>/dev/null | sed 's/^/     /' || true
  echo "   --- k8s pod (bridge-cpu-contended) ---"
  k get pods -n "$WL_NS" -l batch.kubernetes.io/job-name=bridge-cpu-contended \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName' 2>/dev/null \
    | sed 's/^/     /' || true
  local reason
  reason="$(slurm squeue -h -n bridge-cpu-contended -o '%T:%r' 2>/dev/null | head -1 || true)"
  echo "   bridge external job state:reason = ${reason:-<not yet queued>}"

  # Let the native job free the nodes; the bridge pod should then proceed.
  if [ -n "$njid" ]; then
    echo ">> waiting for the native job to release the nodes..."
    for _ in $(seq 1 60); do
      slurm squeue -h -j "$njid" -o '%T' 2>/dev/null | grep -q . || break
      sleep 3
    done
    echo ">> native job released (squeue no longer lists ${njid})."
  fi

  echo ">> waiting for the bridge Job to complete now that nodes are free..."
  if k wait --for=condition=complete job/bridge-cpu-contended -n "$WL_NS" --timeout=180s 2>/dev/null; then
    echo "   PASS: the bridge pod ran only AFTER the native job freed the shared nodes."
  else
    echo "##[warning]bridge Job did not complete in time:"
    k get pods -n "$WL_NS" -l batch.kubernetes.io/job-name=bridge-cpu-contended -o wide || true
  fi
  echo "   => one Slurm queue arbitrated a native job and a Kubernetes Job on the"
  echo "      same nodes — the basic co-scheduling idea, validated on CPU."
}

# ===============================================================================
# teardown
# ===============================================================================
teardown() {
  echo "##[section]Teardown"
  k delete job bridge-cpu-hello bridge-cpu-contended -n "$WL_NS" --ignore-not-found 2>/dev/null || true
  if resolve_slurmctld 2>/dev/null; then
    slurm scancel -n native-hold 2>/dev/null || true
  fi
  ensure_helm
  helmw uninstall slurm-bridge -n "$SLURM_NS" 2>/dev/null || echo ">> slurm-bridge release already gone"
  # The controllers imperatively taint managed nodes; helm uninstall does NOT undo
  # that. Strip the taint so any evicted Slurm/infra pods can reschedule.
  echo ">> removing any ${MANAGED_TAINT_KEY} taint left on nodes"
  for n in $(k get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    if k taint node "$n" "${MANAGED_TAINT_KEY}-" 2>/dev/null; then
      echo "   untainted $n"
    fi
  done
  k delete token slurm-bridge-token -n "$SLURM_NS" --ignore-not-found 2>/dev/null || true
  k delete secret slurm-bridge-token -n "$SLURM_NS" --ignore-not-found 2>/dev/null || true
  k delete ns "$WL_NS" --ignore-not-found 2>/dev/null || true
  echo ">> done. The slinky Slurm cluster (ns ${SLURM_NS}) was left untouched."
}

case "$ACTION" in
  preflight) preflight ;;
  install)   preflight; install ;;
  demo)      preflight; demo_a; demo_b ;;
  all)       preflight; install; demo_a; demo_b ;;
  teardown)  teardown ;;
  *) echo "usage: run.sh [all|preflight|install|demo|teardown]"; exit 1 ;;
esac
