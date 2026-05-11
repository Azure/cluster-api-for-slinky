#!/usr/bin/env bash
# Pin platform components (kube-system + local-path-storage) that are installed as raw
# manifests (not via Helm) to the controller node, so they never land on compute nodes
# and block cluster-autoscaler scale-in.
#
# DaemonSets (calico-node, kube-proxy, prometheus-node-exporter) are intentionally
# NOT pinned — they must run on every node and don't block scale-in.
#
# Usage: KUBECONFIG=path/to/capi-quickstart.kubeconfig ./scripts/pin-platform-pods.sh
set -euo pipefail

PATCH_BODY='{
  "spec": {
    "template": {
      "spec": {
        "nodeSelector": {"slinky.slurm.net/node-type": "controller"},
        "tolerations": [
          {"key": "slinky.slurm.net/controller", "operator": "Exists", "effect": "NoSchedule"},
          {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
          {"key": "CriticalAddonsOnly", "operator": "Exists"}
        ]
      }
    }
  }
}'

echo "Pinning calico-kube-controllers (Deployment in kube-system) to controller..."
kubectl -n kube-system patch deployment calico-kube-controllers --type=strategic -p "$PATCH_BODY"

echo "Pinning coredns (Deployment in kube-system) to controller..."
kubectl -n kube-system patch deployment coredns --type=strategic -p "$PATCH_BODY"

echo "Pinning local-path-provisioner (Deployment in local-path-storage) to controller..."
kubectl -n local-path-storage patch deployment local-path-provisioner --type=strategic -p "$PATCH_BODY"

echo "Done."
