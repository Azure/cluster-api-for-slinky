"""Shared workload-cluster infrastructure intent and metadata helpers."""

from __future__ import annotations

import re
from typing import Any

import pulumi
import pulumi_kubernetes as k8s

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

_CLUSTER_AUTOSCALER_CHART_REPO = "https://kubernetes.github.io/autoscaler"
_CLUSTER_AUTOSCALER_CHART_NAME = "cluster-autoscaler"
_CLUSTER_AUTOSCALER_CHART_VERSION = "9.57.0"
_CLUSTER_AUTOSCALER_WORKLOAD_KUBECONFIG_SECRET_KEY = "value"
_CAPI_NAMESPACE = "default"
_CAPI_INFRASTRUCTURE_API_GROUP = "infrastructure.cluster.x-k8s.io"
_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def _resource_name(tenant: str, suffix: str) -> str:
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", tenant.lower()).strip("-")
    if not normalized:
        raise ValueError("tenant must contain at least one alphanumeric character")
    max_tenant_length = _DNS_LABEL_MAX_LENGTH - len(suffix) - 1
    normalized = normalized[:max_tenant_length].rstrip("-")
    return f"{normalized}-{suffix}"


def _cluster_autoscaler_namespace(instance: str) -> str:
    return _resource_name(instance, "autoscaler")


def _cluster_autoscaler_release_name(instance: str) -> str:
    return _resource_name(instance, "autoscaler")


def _cluster_autoscaler_fullname(instance: str) -> str:
    return _resource_name(instance, "cluster-autoscaler")


def _cluster_autoscaler_kubeconfig_secret_name(instance: str) -> str:
    return _resource_name(instance, "autoscaler-kubeconfig")


def _cluster_autoscaler_values(
    *,
    cluster_name: str,
    fullname: str,
    kubeconfig_secret_name: str,
) -> dict[str, object]:
    return {
        "fullnameOverride": fullname,
        "cloudProvider": "clusterapi",
        "clusterAPIMode": "kubeconfig-incluster",
        "clusterAPIKubeconfigSecret": kubeconfig_secret_name,
        "clusterAPIWorkloadKubeconfigPath": (
            f"/etc/kubernetes/{_CLUSTER_AUTOSCALER_WORKLOAD_KUBECONFIG_SECRET_KEY}"
        ),
        "autoDiscovery": {
            "namespace": _CAPI_NAMESPACE,
            "clusterName": cluster_name,
        },
        "extraArgs": {
            "logtostderr": True,
            "stderrthreshold": "info",
            "v": 4,
            "scale-down-unneeded-time": "2m",
        },
    }


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


class ClusterAPIAutoscaler(pulumi.ComponentResource):
    """Cluster Autoscaler for a CAPI-managed workload cluster."""

    namespace: pulumi.Output[str]
    release_name: pulumi.Output[str]
    status: pulumi.Output[Any]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        cluster_name: str,
        workload_kubeconfig: pulumi.Input[str],
        provider: k8s.Provider,
        infrastructure_api_groups: tuple[str, ...] = (_CAPI_INFRASTRUCTURE_API_GROUP,),
        depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:ClusterAPIAutoscaler",
            name,
            props={},
            opts=opts,
        )

        namespace_name = _cluster_autoscaler_namespace(instance)
        release_name = _cluster_autoscaler_release_name(instance)
        fullname = _cluster_autoscaler_fullname(instance)
        kubeconfig_secret_name = _cluster_autoscaler_kubeconfig_secret_name(instance)
        infrastructure_rbac_name = _resource_name(instance, "autoscaler-infra")

        def child_options(
            *,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
            delete_before_replace: bool | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                delete_before_replace=delete_before_replace,
            )

        namespace = k8s.core.v1.Namespace(
            "namespace",
            metadata={"name": namespace_name},
            opts=child_options(depends_on=depends_on),
        )
        kubeconfig_secret = k8s.core.v1.Secret(
            "workload-kubeconfig",
            metadata={
                "name": kubeconfig_secret_name,
                "namespace": namespace_name,
            },
            type="Opaque",
            string_data={
                _CLUSTER_AUTOSCALER_WORKLOAD_KUBECONFIG_SECRET_KEY: workload_kubeconfig
            },
            opts=child_options(depends_on=[namespace]),
        )
        infrastructure_cluster_role = k8s.rbac.v1.ClusterRole(
            "infrastructure-cluster-role",
            metadata={"name": infrastructure_rbac_name},
            rules=[
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=list(infrastructure_api_groups),
                    resources=["*"],
                    verbs=["get", "list", "watch", "update", "patch"],
                )
            ],
            opts=child_options(depends_on=depends_on),
        )
        k8s.rbac.v1.ClusterRoleBinding(
            "infrastructure-cluster-role-binding",
            metadata={"name": infrastructure_rbac_name},
            role_ref=k8s.rbac.v1.RoleRefArgs(
                api_group="rbac.authorization.k8s.io",
                kind="ClusterRole",
                name=infrastructure_rbac_name,
            ),
            subjects=[
                k8s.rbac.v1.SubjectArgs(
                    kind="ServiceAccount",
                    name=fullname,
                    namespace=namespace_name,
                )
            ],
            opts=child_options(depends_on=[infrastructure_cluster_role, namespace]),
        )
        release = k8s.helm.v3.Release(
            "release",
            chart=_CLUSTER_AUTOSCALER_CHART_NAME,
            name=release_name,
            version=_CLUSTER_AUTOSCALER_CHART_VERSION,
            repository_opts={"repo": _CLUSTER_AUTOSCALER_CHART_REPO},
            namespace=namespace_name,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values=_cluster_autoscaler_values(
                cluster_name=cluster_name,
                fullname=fullname,
                kubeconfig_secret_name=kubeconfig_secret_name,
            ),
            opts=child_options(
                depends_on=[namespace, kubeconfig_secret],
                delete_before_replace=True,
            ),
        )

        self.namespace = pulumi.Output.from_input(namespace_name)
        self.release_name = pulumi.Output.from_input(release_name)
        self.status = release.status
        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_name": self.release_name,
                "status": self.status,
            }
        )