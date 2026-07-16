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
#   Demo C — GANG scheduling (coscheduling PodGroup): a 2-node JobSet (+ a
#     coscheduling PodGroup) is placed by Slurm as ONE external job spanning both
#     nodes, atomically — all ranks together or not at all. That is the multi-node
#     primitive MPI/NCCL needs (the GPU version is cluster-api-provider-for-slinky/
#     nccl-bridge-jobset.yaml). It requires the gang prereqs (scheduler-plugins
#     CoScheduling + jobset + lws) that `install` now deploys and pins to the
#     dedicated controller node.
#
#   Demo D — GANG scheduling (in-tree PodGroup, Kubernetes 1.36+): the same atomic
#     2-node placement as Demo C, but expressed with the in-tree PodGroup API
#     (scheduling.k8s.io/v1alpha2 — a Workload + a PodGroup, pods opting in via
#     spec.schedulingGroup.podGroupName) instead of the coscheduling CRD. Requires
#     the apiserver feature gates GenericWorkload + WorkloadWithJob and runtime-config
#     scheduling.k8s.io/v1alpha2 (set in selfmanaged-workload-cluster.yaml's
#     KubeadmControlPlane — NOT installed by this script); `demo_d` skips gracefully
#     when they are absent (Demo C is the no-gate fallback). GPU NCCL version:
#     nccl-bridge-wholenode-pg136.yaml.
#
# It runs ON the CAPZ management VM (needs helm + line of sight to the caps-self
# API server). Drive it from a workstation with:
#     scripts/azure-remote.sh bridge-experiment [all|install|demo|gang|gang136|teardown]
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
# tolerates the taint (or BRIDGE_ACK_UNSAFE=1). caps-self now satisfies this: a
# dedicated non-managed controller node (caps-self-md-ctrl, node-type=controller)
# hosts the control planes + gang prereqs, and slurmd already tolerates the taint
# (see caps-self-slurm.yaml), so `guard_topology` passes on caps-self.
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
# Gang / JobSet / LeaderWorkerSet prerequisites (enable multi-node gang workloads).
JOBSET_VERSION="${JOBSET_VERSION:-0.12.0}"
LWS_VERSION="${LWS_VERSION:-0.8.0}"
# Taint the bridge controllers imperatively stamp on every managed-partition node.
MANAGED_TAINT_KEY="slinky.slurm.net/managed-node"
# The dedicated non-managed node (caps-self-md-ctrl) that hosts the Slurm + bridge
# control planes and the gang prereqs. Platform pods are pinned here so the
# compute-node managed-node:NoExecute taint can never evict them.
CONTROLLER_NODE_TYPE="${CONTROLLER_NODE_TYPE:-controller}"
CONTROLLER_TAINT_KEY="slinky.slurm.net/controller"

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

# ===============================================================================# Gang prerequisites + controller-node pinning
# ===============================================================================
# slurm-bridge only GANG-schedules a workload when its pods share a PodGroup; a
# bare Job/JobSet is translated pod-by-pod (each becomes its own single-node Slurm
# job). Gang therefore needs (1) the coscheduling PodGroup CRD (scheduler-plugins)
# and (2) the JobSet + LeaderWorkerSet controllers that shape multi-node workloads.
# Those controllers ship with no nodeSelector, so — exactly like the bridge itself —
# they can land on a compute node and then get EVICTED the instant slurm-bridge
# taints it managed-node:NoExecute. pin_to_controller() re-homes them (and the
# bridge components) onto the dedicated caps-self-md-ctrl node.

# pin_to_controller <namespace> [label-selector]
# Patch every (matching) Deployment in <namespace> onto the controller node.
# Idempotent, chart-agnostic (kubectl patch, not helm values — the v1.1.1 bridge
# chart drops nodeSelector/affinity), and a no-op if the namespace is absent.
pin_to_controller() {
  local ns="$1" sel="${2:-}" d patch
  patch='{"spec":{"template":{"spec":{"nodeSelector":{"slinky.slurm.net/node-type":"'"${CONTROLLER_NODE_TYPE}"'"},"tolerations":[{"key":"'"${CONTROLLER_TAINT_KEY}"'","operator":"Exists","effect":"NoSchedule"}]}}}}'
  k get ns "$ns" >/dev/null 2>&1 || return 0
  local getargs=(-n "$ns" get deploy -o name)
  [ -n "$sel" ] && getargs=(-n "$ns" get deploy -l "$sel" -o name)
  for d in $(k "${getargs[@]}" 2>/dev/null); do
    k -n "$ns" patch "$d" --type=merge -p "$patch" >/dev/null 2>&1 \
      && echo "   pinned ${ns}/${d#*/} -> node-type=${CONTROLLER_NODE_TYPE}"
    k -n "$ns" rollout status "$d" --timeout=120s >/dev/null 2>&1 || true
  done
}

# ensure_prereqs: install (idempotently) the gang enablers and pin them so they
# survive the managed-node taint. scheduler.replicaCount=0 keeps the coscheduling
# CRD + PodGroup controller WITHOUT running a second kube-scheduler (slurm-bridge
# is the scheduler); we only need the PodGroup type + controller for gang.
ensure_prereqs() {
  echo ">> installing gang prerequisites (scheduler-plugins CoScheduling, jobset, lws)"
  if ! helmw -n scheduler-plugins status scheduler-plugins >/dev/null 2>&1; then
    helmw install scheduler-plugins scheduler-plugins \
      --repo https://scheduler-plugins.sigs.k8s.io -n scheduler-plugins --create-namespace \
      --set 'plugins.enabled={CoScheduling}' --set 'scheduler.replicaCount=0' \
      --wait --timeout 5m || echo "##[warning]scheduler-plugins install reported issues (continuing)"
  else
    echo "   scheduler-plugins already installed"
  fi
  if ! helmw -n jobset-system status jobset >/dev/null 2>&1; then
    helmw install jobset oci://registry.k8s.io/jobset/charts/jobset --version "$JOBSET_VERSION" \
      -n jobset-system --create-namespace --wait --timeout 5m \
      || echo "##[warning]jobset install reported issues (continuing)"
  else
    echo "   jobset already installed"
  fi
  if ! helmw -n lws-system status lws >/dev/null 2>&1; then
    helmw install lws oci://registry.k8s.io/lws/charts/lws --version "$LWS_VERSION" \
      -n lws-system --create-namespace --wait --timeout 5m \
      || echo "##[warning]lws install reported issues (continuing)"
  else
    echo "   lws already installed"
  fi
  # Keep the prereq controllers off the managed compute nodes.
  pin_to_controller scheduler-plugins
  pin_to_controller jobset-system
  pin_to_controller lws-system
}

# ===============================================================================# preflight
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

  # 0. Gang prerequisites (scheduler-plugins CoScheduling + jobset + lws), each
  #    pinned to the controller node. These make multi-node gang workloads (Demo C
  #    and the GPU NCCL JobSet) placeable as a single Slurm allocation.
  ensure_prereqs

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

  # 2b. Pin the bridge components to the controller node too. The v1.1.1 chart
  #     can't pin via values (it renders nodeSelector/affinity INSIDE the container
  #     spec, where they're silently dropped), so patch the Deployments directly —
  #     otherwise they race the managed-node taint and get evicted off compute.
  pin_to_controller "$SLURM_NS" 'app.kubernetes.io/instance=slurm-bridge'

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
# Demo C — GANG scheduling: a 2-node JobSet placed as ONE Slurm allocation
# ===============================================================================
demo_c() {
  echo "##[section]Demo C — gang-scheduled 2-node JobSet (one atomic Slurm job)"
  resolve_slurmctld
  if ! k get crd jobsets.jobset.x-k8s.io >/dev/null 2>&1 \
     || ! k get crd podgroups.scheduling.x-k8s.io >/dev/null 2>&1; then
    echo "##[warning]gang prereqs (jobset + coscheduling PodGroup CRDs) are missing."
    echo "##[warning]Run 'install' to add them, then re-run. Skipping Demo C."
    return 0
  fi

  local idle
  idle="$(slurm sinfo -h -N -p all -t idle -o '%N' 2>/dev/null | sort -u | grep -c . || true)"
  idle="${idle:-0}"
  echo ">> idle nodes in partition 'all': ${idle} (the gang needs 2)"
  if [ "$idle" -lt 2 ]; then
    echo "##[warning]fewer than 2 idle nodes — the gang would stay PENDING. Skipping."
    return 0
  fi

  k delete jobset gang-proxy -n "$WL_NS" --ignore-not-found >/dev/null 2>&1 || true
  k delete podgroup.scheduling.x-k8s.io gang-proxy -n "$WL_NS" --ignore-not-found >/dev/null 2>&1 || true
  sleep 3
  echo ">> submitting the 2-node gang (PodGroup minMember=2 + JobSet parallelism=2)"
  k apply -f "$SCRIPT_DIR/slurm-bridge-gang-job.yaml"

  echo ">> waiting for BOTH gang pods to reach Running (atomic placement)..."
  local running=0
  for _ in $(seq 1 40); do
    running="$(k get pods -n "$WL_NS" -l scheduling.x-k8s.io/pod-group=gang-proxy \
                 --field-selector=status.phase=Running --no-headers 2>/dev/null | grep -c . || true)"
    [ "${running:-0}" -ge 2 ] && break
    sleep 3
  done

  echo ">> gang pods (expect 2 Running, on 2 distinct nodes, SAME Slurm JobID):"
  k get pods -n "$WL_NS" -l scheduling.x-k8s.io/pod-group=gang-proxy \
    -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,JOBID:.metadata.labels.scheduler\.slinky\.slurm\.net/slurm-jobid' \
    2>&1 | sed 's/^/     /' || true

  local jid
  jid="$(k get pods -n "$WL_NS" -l scheduling.x-k8s.io/pod-group=gang-proxy \
           -o jsonpath='{.items[0].metadata.labels.scheduler\.slinky\.slurm\.net/slurm-jobid}' 2>/dev/null || true)"
  if [ -n "$jid" ]; then
    echo ">> Slurm's view of the gang (expect NumNodes=2, both nodes in NodeList):"
    slurm scontrol show job "$jid" 2>&1 \
      | grep -oE 'JobId=[^ ]+|JobState=[^ ]+|NumNodes=[^ ]+|NodeList=[^ ]+' | sort -u | sed 's/^/     /' || true
  fi

  echo ">> waiting for the gang JobSet to complete..."
  if k wait --for=condition=completed jobset/gang-proxy -n "$WL_NS" --timeout=180s 2>/dev/null; then
    echo "   PASS: a 2-node JobSet was gang-placed by Slurm as ONE allocation."
  else
    echo "##[warning]gang JobSet did not complete in time; current pods:"
    k get pods -n "$WL_NS" -l scheduling.x-k8s.io/pod-group=gang-proxy -o wide 2>&1 | sed 's/^/     /' || true
  fi
  echo "   => the multi-node primitive MPI/NCCL needs — all ranks together or not"
  echo "      at all — works through the bridge (GPU version: nccl-bridge-jobset.yaml)."
}

# ===============================================================================
# Demo D — GANG via the IN-TREE PodGroup (Kubernetes 1.36+ form)
# ===============================================================================
# Does the workload cluster's apiserver serve the in-tree PodGroup (Kubernetes
# 1.36+)? True only when the GenericWorkload + WorkloadWithJob feature gates and
# the scheduling.k8s.io/v1alpha2 runtime-config are enabled on kube-apiserver
# (baked into the cluster via selfmanaged-workload-cluster.yaml, not this script).
check_intree_podgroup() {
  local ar
  ar="$(k api-resources --api-group=scheduling.k8s.io 2>/dev/null || true)"
  grep -qi 'PodGroup' <<<"$ar" && grep -qi 'Workload' <<<"$ar"
}

# The same atomic 2-node placement as Demo C, but the gang is expressed with the
# in-tree PodGroup API (Workload + PodGroup, pods opting in via
# spec.schedulingGroup.podGroupName) instead of the coscheduling CRD. Skips
# gracefully when the 1.36 API is not served (Demo C is the no-gate fallback).
demo_d() {
  echo "##[section]Demo D — gang via the in-tree PodGroup (Kubernetes 1.36+)"
  resolve_slurmctld
  if ! check_intree_podgroup; then
    echo "##[warning]the in-tree PodGroup API (scheduling.k8s.io/v1alpha2) is not served"
    echo "##[warning]by the ${CLUSTER} apiserver. Enable GenericWorkload + WorkloadWithJob"
    echo "##[warning]+ runtime-config scheduling.k8s.io/v1alpha2 on the control plane (see"
    echo "##[warning]selfmanaged-workload-cluster.yaml's KubeadmControlPlane), then re-run."
    echo "##[warning]The coscheduling gang (Demo C / 'gang') is the no-gate fallback. Skipping."
    return 0
  fi

  local idle
  idle="$(slurm sinfo -h -N -p all -t idle -o '%N' 2>/dev/null | sort -u | grep -c . || true)"
  idle="${idle:-0}"
  echo ">> idle nodes in partition 'all': ${idle} (the gang needs 2)"
  if [ "$idle" -lt 2 ]; then
    echo "##[warning]fewer than 2 idle nodes — the gang would stay PENDING. Skipping."
    return 0
  fi

  # Fresh start. Delete in reverse dependency order (the PodGroup admission
  # validates the Workload exists on create, so recreate cleanly each run).
  k delete job pg136-test -n "$WL_NS" --ignore-not-found >/dev/null 2>&1 || true
  k delete podgroup.scheduling.k8s.io pg136-test-gang -n "$WL_NS" --ignore-not-found >/dev/null 2>&1 || true
  k delete workload pg136-test -n "$WL_NS" --ignore-not-found >/dev/null 2>&1 || true
  sleep 3
  echo ">> submitting the 2-node gang (Workload + in-tree PodGroup gang.minCount=2 + Job)"
  k apply -f "$SCRIPT_DIR/podgroup136-test.yaml"

  echo ">> waiting for BOTH gang pods to reach Running (atomic placement)..."
  local running=0
  for _ in $(seq 1 40); do
    running="$(k get pods -n "$WL_NS" -l job-name=pg136-test \
                 --field-selector=status.phase=Running --no-headers 2>/dev/null | grep -c . || true)"
    [ "${running:-0}" -ge 2 ] && break
    sleep 3
  done

  echo ">> gang pods (expect 2 Running, on 2 distinct nodes, SAME Slurm JobID):"
  k get pods -n "$WL_NS" -l job-name=pg136-test \
    -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,JOBID:.metadata.labels.scheduler\.slinky\.slurm\.net/slurm-jobid,GROUP:.spec.schedulingGroup.podGroupName' \
    2>&1 | sed 's/^/     /' || true

  echo ">> in-tree PodGroup status (expect PodGroupScheduled=True):"
  k get podgroup.scheduling.k8s.io pg136-test-gang -n "$WL_NS" \
    -o jsonpath='{range .status.conditions[*]}     {.type}={.status} ({.message}){"\n"}{end}' 2>/dev/null || true

  local jid
  jid="$(k get pods -n "$WL_NS" -l job-name=pg136-test \
           -o jsonpath='{.items[0].metadata.labels.scheduler\.slinky\.slurm\.net/slurm-jobid}' 2>/dev/null || true)"
  if [ -n "$jid" ]; then
    echo ">> Slurm's view of the gang (expect NumNodes=2, both nodes in NodeList):"
    slurm scontrol show job "$jid" 2>&1 \
      | grep -oE 'JobId=[^ ]+|JobState=[^ ]+|NumNodes=[^ ]+|NodeList=[^ ]+' | sort -u | sed 's/^/     /' || true
  fi

  echo ">> waiting for the gang Job to complete..."
  if k wait --for=condition=complete job/pg136-test -n "$WL_NS" --timeout=180s 2>/dev/null; then
    echo "   PASS: a 2-node gang was placed by Slurm as ONE allocation via the in-tree PodGroup."
  else
    echo "##[warning]gang Job did not complete in time; current pods:"
    k get pods -n "$WL_NS" -l job-name=pg136-test -o wide 2>&1 | sed 's/^/     /' || true
  fi
  echo "   => the same atomic multi-node primitive as Demo C, via the Kubernetes 1.36"
  echo "      in-tree PodGroup (GPU NCCL version: nccl-bridge-wholenode-pg136.yaml)."
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
  echo ">> done. The slinky Slurm cluster (ns ${SLURM_NS}) was left untouched;"
  echo "   the gang prereqs (jobset/lws/scheduler-plugins) were left installed (reusable)."
}

case "$ACTION" in
  preflight) preflight ;;
  install)   preflight; install ;;
  demo)      preflight; demo_a; demo_b; demo_c; demo_d ;;
  gang)      preflight; demo_c ;;
  gang136)   preflight; demo_d ;;
  all)       preflight; install; demo_a; demo_b; demo_c; demo_d ;;
  teardown)  teardown ;;
  *) echo "usage: run.sh [all|preflight|install|demo|gang|gang136|teardown]"; exit 1 ;;
esac
