"""Shared workload-cluster infrastructure intent and metadata helpers."""

from __future__ import annotations

NODE_TYPE_LABEL = "slinky.slurm.net/node-type"
CONTROLLER_NODE_TYPE = "controller"
COMPUTE_NODE_TYPE = "compute"
AUTOSCALER_MIN_ANNOTATION = (
    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size"
)
AUTOSCALER_MAX_ANNOTATION = (
    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size"
)
POD_SECURITY_PRIVILEGED_LABELS = {
    "pod-security.kubernetes.io/enforce": "privileged",
    "pod-security.kubernetes.io/enforce-version": "latest",
}

CLUSTER_AUTOSCALER_DISCOVERY_LABEL = "ca4s.azure.com/autoscaler-enabled"
CLUSTER_AUTOSCALER_DISCOVERY_LABEL_VALUE = "true"


def worker_labels(cluster_name: str, node_type: str) -> dict[str, str]:
    return {
        "cluster.x-k8s.io/cluster-name": cluster_name,
        NODE_TYPE_LABEL: node_type,
    }


def _autoscaler_discovery_labels(enabled: bool) -> dict[str, str]:
    if enabled:
        return {
            CLUSTER_AUTOSCALER_DISCOVERY_LABEL: (
                CLUSTER_AUTOSCALER_DISCOVERY_LABEL_VALUE
            )
        }
    return {}


def machine_deployment_labels(
    cluster_name: str,
    node_type: str,
    *,
    autoscaler_enabled: bool = False,
) -> dict[str, str]:
    return {
        **worker_labels(cluster_name, node_type),
        **_autoscaler_discovery_labels(autoscaler_enabled),
    }


def node_labels(node_type: str) -> dict[str, str]:
    return {NODE_TYPE_LABEL: node_type}