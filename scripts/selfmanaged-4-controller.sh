#!/usr/bin/env bash
#
# selfmanaged-4-controller.sh — bring up the dedicated in-cluster controller node
# (the `<cluster>-md-ctrl` MachineDeployment), then optionally repin the Slurm
# head pods onto it and taint the GPU compute nodes.
#
# Parameterized by cluster (default caps-self). Runs ON the mgmt VM (default
# kubectl context = the mgmt kind cluster); workload-cluster steps use the
# `<cluster>-kubeconfig` secret. Idempotent + safe to re-run (e.g. if the
# node-provisioning wait times out, just run it again). Override for e.g. the
# caps-val validation:
#   CLUSTER=caps-val FLEX_MANIFEST=$PWD/selfmanaged-validation.flex.yaml \
#   VALUES=$PWD/selfmanaged-validation-slurm.yaml \
#   bash scripts/selfmanaged-4-controller.sh
#
#   bash scripts/azure-remote.sh sync
#   bash scripts/azure-remote.sh ssh 'bash scripts/selfmanaged-4-controller.sh'            # PHASE 1 (safe/additive)
#   bash scripts/azure-remote.sh ssh 'DO_REPIN=1 bash scripts/selfmanaged-4-controller.sh' # PHASE 2 (disruptive)
#
# PHASE 1 (default): CAPI label-sync patch + `kubectl apply` the flex manifest +
#   wait for the controller VM to provision, join, and become Ready, and ensure
#   it carries the node-type=controller label. Purely additive — the running
#   compute workers and Slurm are untouched.
# PHASE 2 (DO_REPIN=1): `helm upgrade` Slurm so slurmctld/slurmrestd move onto the
#   controller node, then taint the live compute nodes. This restarts the Slurm
#   control plane. If slurmctld then sticks Pending on a "volume node affinity
#   conflict" (its local-path statesave PVC is pinned to its old node), STEP 6b
#   auto-heals it: it deletes the stranded PVC + pod so local-path reprovisions on
#   the controller node (Slurm state is experiment-disposable here). The heal is
#   guarded — it only fires on that exact Pending reason.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${CLUSTER:-caps-self}"
NAMESPACE="${NAMESPACE:-default}"
FLEX_MANIFEST="${FLEX_MANIFEST:-$REPO/selfmanaged-workload-cluster.flex.yaml}"
# VALUES aligns with selfmanaged-5-slurm.sh; SLURM_VALUES kept as a fallback alias.
SLURM_VALUES="${VALUES:-${SLURM_VALUES:-$REPO/caps-self-slurm.yaml}}"
KC="${KC:-/tmp/${CLUSTER}.kubeconfig}"
SLURM_NS="${SLURM_NS:-slurm}"
SLURM_RELEASE="${SLURM_RELEASE:-slurm}"
SLURM_CHART="oci://ghcr.io/slinkyproject/charts/slurm"
SLURM_CHART_VERSION="${SLURM_CHART_VERSION:-1.1.1}"
MD_LABEL="cluster.x-k8s.io/deployment-name=${CLUSTER}-md-ctrl"
DO_REPIN="${DO_REPIN:-0}"

# helm is installed under ~/.local/bin on the mgmt VM; a non-login ssh shell may
# not have it on PATH.
HELM="${HELM:-}"
[[ -n "$HELM" ]] || HELM="$(command -v helm || true)"
[[ -x "$HELM" ]] || HELM="$HOME/.local/bin/helm"

echo "############ STEP 1: CAPI Machine->Node label sync patch (mgmt) ############"
if kubectl -n capi-system get deploy capi-controller-manager -o yaml | grep -q 'additional-sync-machine-labels'; then
  echo "   label-sync arg already present — skipping patch"
else
  # NOTE the double backslashes: inside bash single-quotes they stay literal, so
  # kubectl receives valid JSON (\\ -> one backslash), yielding the regex
  # ...slinky\.slurm\.net... A single backslash here is an invalid JSON escape.
  kubectl -n capi-system patch deployment/capi-controller-manager --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--additional-sync-machine-labels=.*slinky\\.slurm\\.net.*"}]'
  kubectl -n capi-system rollout status deploy/capi-controller-manager --timeout=180s
fi

echo "############ STEP 2: apply flex manifest (mgmt) ############"
# Creates the <cluster>-md-ctrl trio and updates the <cluster>-md-0
# KubeadmConfigTemplate with the compute taint (affects FUTURE compute VMs only;
# live workers are tainted in PHASE 2). The compute MachineDeployment spec is
# unchanged, so no compute rollout happens.
kubectl apply -f "$FLEX_MANIFEST"

echo "############ STEP 3: wait for the controller Machine to reach Running ############"
# The MachineDeployment -> MachineSet -> Machine chain takes a moment to appear,
# then CAPZ has to create + kubeadm-join an Azure VM.
for i in $(seq 1 90); do
  phase="$(kubectl get machine -l "$MD_LABEL" -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)"
  echo "   [$i/90] controller Machine phase=${phase:-<none>}"
  [[ "$phase" == "Running" ]] && break
  [[ "$phase" == "Failed" ]] && { echo "   Machine Failed — inspect with 'kubectl describe machine -l $MD_LABEL'"; exit 1; }
  sleep 20
done
kubectl get machine -l "$MD_LABEL" -o wide || true

echo "############ STEP 4: fetch workload kubeconfig ############"
kubectl -n "$NAMESPACE" get secret "${CLUSTER}-kubeconfig" -o jsonpath='{.data.value}' | base64 -d > "$KC"
chmod 600 "$KC"

echo "############ STEP 5: wait for controller node Ready + ensure label ############"
node="$(kubectl get machine -l "$MD_LABEL" -o jsonpath='{.items[0].status.nodeRef.name}' 2>/dev/null || true)"
echo "   controller node = ${node:-<pending>}"
if [[ -z "$node" ]]; then
  echo "   node not registered yet — re-run this script once the Machine is Running." >&2
  exit 0
fi
# Fallback label in case the CAPI label-sync hasn't landed it yet.
if ! KUBECONFIG="$KC" kubectl get node "$node" -o jsonpath='{.metadata.labels.slinky\.slurm\.net/node-type}' 2>/dev/null | grep -q controller; then
  echo "   node-type label not synced yet; applying manually"
  KUBECONFIG="$KC" kubectl label node "$node" slinky.slurm.net/node-type=controller --overwrite
fi
KUBECONFIG="$KC" kubectl wait --for=condition=Ready node/"$node" --timeout=600s
echo "   controller node Ready:"
KUBECONFIG="$KC" kubectl get nodes -L slinky.slurm.net/node-type

if [[ "$DO_REPIN" != "1" ]]; then
  echo
  echo ">> PHASE 1 complete. Controller node is up. Re-run with DO_REPIN=1 to"
  echo ">> repin Slurm head pods onto it and taint the compute nodes."
  exit 0
fi

echo "############ STEP 6: repin Slurm head pods (helm upgrade) ############"
if ! KUBECONFIG="$KC" "$HELM" -n "$SLURM_NS" status "$SLURM_RELEASE" >/dev/null 2>&1; then
  echo "   ERROR: helm release '$SLURM_RELEASE' not found in ns '$SLURM_NS'. Releases:" >&2
  KUBECONFIG="$KC" "$HELM" -n "$SLURM_NS" list >&2 || true
  exit 1
fi
KUBECONFIG="$KC" "$HELM" upgrade "$SLURM_RELEASE" "$SLURM_CHART" --version "$SLURM_CHART_VERSION" \
  -n "$SLURM_NS" -f "$SLURM_VALUES"

echo "############ STEP 6b: heal slurmctld if its node-local PVC pins it to the old node ############"
# Moving slurm-controller-0 onto the controller node can strand its local-path
# statesave PVC on the previous node (WaitForFirstConsumer => node-local PV),
# leaving the pod Pending with "volume node affinity conflict". A local-path PV
# cannot migrate, so re-provision: delete the PVC + pod (Slurm state is
# experiment-disposable here) and let the StatefulSet + local-path recreate them
# on the controller node. GUARDED: only acts on that exact Pending reason; a pod
# that is Running (or Pending for any other reason) is left untouched.
heal_controller_pvc() {
  local pod="slurm-controller-0" sts="slurm-controller" msg="" phase="" i
  for i in $(seq 1 12); do
    phase="$(KUBECONFIG="$KC" kubectl -n "$SLURM_NS" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "$phase" == "Running" ]]; then
      echo "   $pod is Running — no remediation needed."
      return 0
    fi
    msg="$(KUBECONFIG="$KC" kubectl -n "$SLURM_NS" get pod "$pod" \
             -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}' 2>/dev/null || true)"
    if echo "$msg" | grep -qi 'volume node affinity conflict'; then
      local pvc
      pvc="$(KUBECONFIG="$KC" kubectl -n "$SLURM_NS" get pod "$pod" \
               -o jsonpath='{.spec.volumes[*].persistentVolumeClaim.claimName}' 2>/dev/null || true)"
      echo "   detected volume node affinity conflict; reprovisioning:"
      echo "     message: $msg"
      echo "     stranded PVC(s): ${pvc:-<none>}"
      # Delete PVC first (pvc-protection keeps it Terminating until the pod that
      # references it is gone), then the pod; the StatefulSet + volumeClaimTemplate
      # recreate both, and local-path binds a fresh PV on the controller node.
      for c in $pvc; do
        KUBECONFIG="$KC" kubectl -n "$SLURM_NS" delete pvc "$c" --wait=false 2>/dev/null || true
      done
      KUBECONFIG="$KC" kubectl -n "$SLURM_NS" delete pod "$pod" --wait=false 2>/dev/null || true
      break
    fi
    echo "   [$i/12] $pod phase=${phase:-<none>} — waiting for it to settle before deciding"
    sleep 10
  done
  echo "   waiting for slurmctld to come Ready on the controller node..."
  KUBECONFIG="$KC" kubectl -n "$SLURM_NS" rollout status "statefulset/$sts" --timeout=300s 2>/dev/null \
    || KUBECONFIG="$KC" kubectl -n "$SLURM_NS" wait --for=condition=Ready "pod/$pod" --timeout=300s 2>/dev/null \
    || echo "   ##[warning]$pod not Ready yet — inspect with 'kubectl -n $SLURM_NS describe pod $pod'"
}
heal_controller_pvc

echo "############ STEP 7: taint the live compute nodes ############"
# Use bare node names: `kubectl taint` does not accept the `node/<name>` form.
for n in $(KUBECONFIG="$KC" kubectl get nodes -l slinky.slurm.net/node-type=compute -o jsonpath='{.items[*].metadata.name}'); do
  echo "   tainting $n"
  KUBECONFIG="$KC" kubectl taint node "$n" slinky.slurm.net/compute=:NoSchedule --overwrite
done

echo "############ DONE ############"
KUBECONFIG="$KC" kubectl get nodes -L slinky.slurm.net/node-type
echo "--- slurm pods (slurmctld/slurmrestd should be on the controller node) ---"
KUBECONFIG="$KC" kubectl -n "$SLURM_NS" get pods -o wide
echo "NOTE: STEP 6b already auto-heals a slurmctld 'volume node affinity conflict'."
echo "      If slurm-controller-0 is still not Ready, inspect it:"
echo "      KUBECONFIG=$KC kubectl -n $SLURM_NS describe pod slurm-controller-0"
