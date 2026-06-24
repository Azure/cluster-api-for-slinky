"""Common Kubernetes deployments installed onto workload clusters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pulumi
import pulumi_kubernetes as k8s

from stacks.workload_cluster.workload_cluster_infrastructure import (
    CONTROLLER_NODE_TYPE,
    NODE_TYPE_LABEL,
    POD_SECURITY_PRIVILEGED_LABELS,
)


_CERT_MANAGER_CHART_REPO = "https://charts.jetstack.io"
_CERT_MANAGER_CHART_NAME = "cert-manager"
_CERT_MANAGER_CHART_VERSION = "v1.20.2"
_CERT_MANAGER_NAMESPACE = "cert-manager"

_PROMETHEUS_CHART_REPO = "https://prometheus-community.github.io/helm-charts"
_PROMETHEUS_CHART_NAME = "kube-prometheus-stack"
_PROMETHEUS_CHART_VERSION = "86.2.2"
_PROMETHEUS_NAMESPACE = "prometheus"

_KEDA_CHART_REPO = "https://kedacore.github.io/charts"
_KEDA_CHART_NAME = "keda"
_KEDA_CHART_VERSION = "2.20.1"
_KEDA_SCALED_OBJECT_API_VERSION = "keda.sh/v1alpha1"
_SLURM_NODESET_API_VERSION = "slinky.slurm.net/v1beta1"
_SLURM_PENDING_JOBS_QUERY = 'sum(slurm_partition_jobs_pending{partition="all"})'
_PROMETHEUS_PORT = 9090

_SLINKY_CHART_OCI_PREFIX = "oci://ghcr.io/slinkyproject/charts"
_SLINKY_CHART_VERSION = "1.1.1"
_SLINKY_OPERATOR_CRDS_CHART = f"{_SLINKY_CHART_OCI_PREFIX}/slurm-operator-crds"
_SLINKY_OPERATOR_CHART = f"{_SLINKY_CHART_OCI_PREFIX}/slurm-operator"
_SLURM_CHART = f"{_SLINKY_CHART_OCI_PREFIX}/slurm"
_SLINKY_OPERATOR_NAMESPACE = "slinky"
_SLURM_NAMESPACE = "slurm"


@dataclass(frozen=True)
class SlurmNodeSetSpec:
    name: str
    node_type: str
    replicas: int


@dataclass(frozen=True)
class KEDANodeSetScalerSpec:
    node_set_name: str
    min_replicas: int
    max_replicas: int


def _keda_namespace(instance: str) -> str:
    return _resource_name(instance, "keda")


def _keda_release_name(instance: str) -> str:
    return _resource_name(instance, "keda")


def _keda_scaled_object_name(instance: str, worker_name: str) -> str:
    return _resource_name(instance, f"{worker_name}-nodeset-scaler")


def _resource_name(tenant: str, suffix: str) -> str:
    import re

    dns_label_invalid_chars = re.compile(r"[^a-z0-9]+")
    dns_label_max_length = 63
    normalized = dns_label_invalid_chars.sub("-", tenant.lower()).strip("-")
    if not normalized:
        raise ValueError("tenant must contain at least one alphanumeric character")
    max_tenant_length = dns_label_max_length - len(suffix) - 1
    normalized = normalized[:max_tenant_length].rstrip("-")
    return f"{normalized}-{suffix}"


def _slurm_nodeset_name(slurm_release_name: str, worker_name: str) -> str:
    return f"{slurm_release_name}-worker-{worker_name}"


def _prometheus_service_name(prometheus_release_name: str) -> str:
    return f"{prometheus_release_name}-kube-p-prometheus"


def _prometheus_server_address(prometheus_release_name: str) -> str:
    return (
        f"http://{_prometheus_service_name(prometheus_release_name)}."
        f"{_PROMETHEUS_NAMESPACE}.svc.cluster.local:{_PROMETHEUS_PORT}"
    )


def _node_type_affinity(node_type: str) -> dict[str, object]:
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": NODE_TYPE_LABEL,
                                "operator": "In",
                                "values": [node_type],
                            }
                        ]
                    }
                ]
            }
        }
    }


def _controller_tolerations() -> list[dict[str, str]]:
    return [
        {
            "key": "slinky.slurm.net/controller",
            "operator": "Exists",
            "effect": "NoSchedule",
        }
    ]


def _controller_pod_spec() -> dict[str, object]:
    return {
        "tolerations": _controller_tolerations(),
        "affinity": _node_type_affinity(CONTROLLER_NODE_TYPE),
    }


def _keda_values() -> dict[str, object]:
    controller_node_selector = {NODE_TYPE_LABEL: CONTROLLER_NODE_TYPE}
    controller_tolerations = _controller_tolerations()
    component_placement = {
        "nodeSelector": controller_node_selector,
        "tolerations": controller_tolerations,
    }
    return {
        **component_placement,
        "metricsServer": component_placement,
        "webhooks": component_placement,
    }


def _keda_scaled_object_spec(
    *,
    node_set_name: pulumi.Input[str],
    min_replicas: int,
    max_replicas: int,
    prometheus_server_address: pulumi.Input[str],
) -> dict[str, object]:
    return {
        "scaleTargetRef": {
            "apiVersion": _SLURM_NODESET_API_VERSION,
            "kind": "NodeSet",
            "name": node_set_name,
        },
        "minReplicaCount": min_replicas,
        "maxReplicaCount": max_replicas,
        "triggers": [
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": prometheus_server_address,
                    "query": _SLURM_PENDING_JOBS_QUERY,
                    "threshold": "1",
                    "activationThreshold": "1",
                    "unsafeSsl": "true",
                },
            }
        ],
    }


def _slurm_operator_values() -> dict[str, object]:
    return {
        "operator": {
            "tolerations": _controller_tolerations(),
            "affinity": _node_type_affinity(CONTROLLER_NODE_TYPE),
        },
        "webhook": {
            "tolerations": _controller_tolerations(),
            "affinity": _node_type_affinity(CONTROLLER_NODE_TYPE),
        },
    }


def _prometheus_values() -> dict[str, object]:
    controller_node_selector = {NODE_TYPE_LABEL: CONTROLLER_NODE_TYPE}
    controller_tolerations = _controller_tolerations()
    return {
        "prometheus": {
            "prometheusSpec": {
                "serviceMonitorSelectorNilUsesHelmValues": False,
                "podMonitorSelectorNilUsesHelmValues": False,
                "nodeSelector": controller_node_selector,
                "tolerations": controller_tolerations,
            },
        },
        "alertmanager": {
            "alertmanagerSpec": {
                "nodeSelector": controller_node_selector,
                "tolerations": controller_tolerations,
            },
        },
        "prometheusOperator": {
            "nodeSelector": controller_node_selector,
            "tolerations": controller_tolerations,
            "admissionWebhooks": {
                "patch": {
                    "nodeSelector": controller_node_selector,
                    "tolerations": controller_tolerations,
                },
            },
        },
        "grafana": {
            "nodeSelector": controller_node_selector,
            "tolerations": controller_tolerations,
        },
        "kube-state-metrics": {
            "nodeSelector": controller_node_selector,
            "tolerations": controller_tolerations,
        },
    }


def _slurm_nodeset_values(node_set: SlurmNodeSetSpec) -> dict[str, object]:
    return {
        "enabled": True,
        "replicas": node_set.replicas,
        "slurmd": {
            "image": {
                "repository": "ghcr.io/slinkyproject/slurmd",
                "tag": "25.11-ubuntu24.04",
            },
            "args": [],
            "resources": {},
            "volumeMounts": [],
        },
        "logfile": {
            "image": {
                "repository": "public.ecr.aws/docker/library/alpine",
                "tag": "3.21",
            },
        },
        "partition": {"enabled": True, "configMap": {}},
        "useResourceLimits": True,
        "updateStrategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxUnavailable": "25%"},
        },
        "taintKubeNodes": False,
        "podSpec": {
            "affinity": {
                **_node_type_affinity(node_set.node_type),
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {
                                "matchExpressions": [
                                    {
                                        "key": "app.kubernetes.io/name",
                                        "operator": "In",
                                        "values": ["slurmd"],
                                    }
                                ]
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                },
            },
        },
    }


def _slurm_values(node_sets: tuple[SlurmNodeSetSpec, ...]) -> dict[str, object]:
    return {
        "configFiles": {
            "cgroup.conf": "CgroupPlugin=disabled\n",
        },
        "controller": {
            "logfile": {
                "image": {
                    "repository": "public.ecr.aws/docker/library/alpine",
                    "tag": "3.21",
                },
            },
            "metrics": {
                "enabled": True,
                "serviceMonitor": {"enabled": True},
            },
            "podSpec": _controller_pod_spec(),
        },
        "loginsets": {
            "slinky": {
                "enabled": True,
                "initconf": {
                    "image": {
                        "repository": "public.ecr.aws/docker/library/alpine",
                        "tag": "3.21",
                    },
                },
                "sssdConf": """[sssd]
config_file_version = 2
services = nss,pam
domains = LOCAL

[nss]
filter_groups = root,slurm
filter_users = root,slurm

[pam]

[domain/LOCAL]
id_provider = files
auth_provider = none
""",
                "podSpec": _controller_pod_spec(),
            },
        },
        "nodesets": {
            "slinky": {"enabled": False},
            **{
                node_set.name: _slurm_nodeset_values(node_set)
                for node_set in node_sets
            },
        },
        "restapi": {"podSpec": _controller_pod_spec()},
    }


class KEDANodeSetScaler(pulumi.ComponentResource):
    """KEDA ScaledObjects that translate Slurm queue depth into NodeSet replicas."""

    namespace: pulumi.Output[str]
    release_name: pulumi.Output[str]
    scaled_object_names: pulumi.Output[list[str]]
    status: pulumi.Output[Any]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        prometheus_release_name: pulumi.Input[str],
        slurm_release_name: pulumi.Input[str],
        scaled_node_sets: tuple[KEDANodeSetScalerSpec, ...],
        provider: k8s.Provider,
        depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:KEDANodeSetScaler",
            name,
            props={},
            opts=opts,
        )

        if not scaled_node_sets:
            raise ValueError("KEDANodeSetScaler requires at least one scaled NodeSet")

        namespace_name = _keda_namespace(instance)
        release_name = _keda_release_name(instance)
        prometheus_server_address = pulumi.Output.from_input(
            prometheus_release_name
        ).apply(_prometheus_server_address)

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
        release = k8s.helm.v3.Release(
            "release",
            chart=_KEDA_CHART_NAME,
            name=release_name,
            version=_KEDA_CHART_VERSION,
            repository_opts={"repo": _KEDA_CHART_REPO},
            namespace=namespace_name,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values=_keda_values(),
            opts=child_options(
                depends_on=[namespace],
                delete_before_replace=True,
            ),
        )

        scaled_object_names: list[str] = []
        for scaled_node_set in scaled_node_sets:
            scaled_object_name = _keda_scaled_object_name(instance, scaled_node_set.node_set_name)
            scaled_object_names.append(scaled_object_name)
            node_set_name = pulumi.Output.from_input(slurm_release_name).apply(
                lambda resolved_release_name, worker_name=scaled_node_set.node_set_name: _slurm_nodeset_name(
                    resolved_release_name,
                    worker_name,
                )
            )
            k8s.apiextensions.CustomResource(
                f"{scaled_node_set.node_set_name}-scaled-object",
                api_version=_KEDA_SCALED_OBJECT_API_VERSION,
                kind="ScaledObject",
                metadata={
                    "name": scaled_object_name,
                    "namespace": _SLURM_NAMESPACE,
                },
                spec=_keda_scaled_object_spec(
                    node_set_name=node_set_name,
                    min_replicas=scaled_node_set.min_replicas,
                    max_replicas=scaled_node_set.max_replicas,
                    prometheus_server_address=prometheus_server_address,
                ),
                opts=child_options(depends_on=[release, *(depends_on or [])]),
            )

        self.namespace = pulumi.Output.from_input(namespace_name)
        self.release_name = pulumi.Output.from_input(release_name)
        self.scaled_object_names = pulumi.Output.from_input(scaled_object_names)
        self.status = release.status
        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_name": self.release_name,
                "scaled_object_names": self.scaled_object_names,
                "status": self.status,
            }
        )


class WorkloadClusterDeployments(pulumi.ComponentResource):
    """Kubernetes deployments installed after a workload cluster exists."""

    keda_namespace: pulumi.Output[str | None]
    keda_scaled_object_names: pulumi.Output[list[str]]
    keda_status: pulumi.Output[Any]
    prometheus_namespace: pulumi.Output[str]
    prometheus_status: pulumi.Output[Any]
    slurm_operator_status: pulumi.Output[Any]
    slurm_status: pulumi.Output[Any]
    workload_cluster_ready: pulumi.Output[bool]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        slurm_node_sets: tuple[SlurmNodeSetSpec, ...],
        keda_scaled_node_sets: tuple[KEDANodeSetScalerSpec, ...] = (),
        workload_provider: k8s.Provider,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:WorkloadClusterDeployments",
            name,
            props={},
            opts=opts,
        )

        def child_options(
            *,
            provider: pulumi.ProviderResource | None = None,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
            retain_on_delete: bool | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
                retain_on_delete=retain_on_delete,
            )

        cert_manager_namespace = k8s.core.v1.Namespace(
            "workload-cert-manager-namespace",
            metadata={"name": _CERT_MANAGER_NAMESPACE},
            opts=child_options(
                provider=workload_provider,
                retain_on_delete=True,
            ),
        )
        cert_manager = k8s.helm.v3.Release(
            "workload-cert-manager",
            chart=_CERT_MANAGER_CHART_NAME,
            version=_CERT_MANAGER_CHART_VERSION,
            repository_opts={"repo": _CERT_MANAGER_CHART_REPO},
            namespace=_CERT_MANAGER_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values={"crds": {"enabled": True}},
            opts=child_options(
                provider=workload_provider,
                depends_on=[cert_manager_namespace],
                retain_on_delete=True,
            ),
        )

        prometheus_namespace = k8s.core.v1.Namespace(
            "prometheus-namespace",
            metadata={
                "name": _PROMETHEUS_NAMESPACE,
                "labels": POD_SECURITY_PRIVILEGED_LABELS,
            },
            opts=child_options(
                provider=workload_provider,
                retain_on_delete=True,
            ),
        )
        prometheus = k8s.helm.v3.Release(
            "prometheus",
            chart=_PROMETHEUS_CHART_NAME,
            version=_PROMETHEUS_CHART_VERSION,
            repository_opts={"repo": _PROMETHEUS_CHART_REPO},
            namespace=_PROMETHEUS_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=900,
            values=_prometheus_values(),
            opts=child_options(
                provider=workload_provider,
                depends_on=[prometheus_namespace],
                retain_on_delete=True,
            ),
        )

        slinky_namespace = k8s.core.v1.Namespace(
            "slinky-operator-namespace",
            metadata={
                "name": _SLINKY_OPERATOR_NAMESPACE,
                "labels": POD_SECURITY_PRIVILEGED_LABELS,
            },
            opts=child_options(
                provider=workload_provider,
                depends_on=[prometheus],
                retain_on_delete=True,
            ),
        )
        slurm_namespace = k8s.core.v1.Namespace(
            "slurm-namespace",
            metadata={
                "name": _SLURM_NAMESPACE,
                "labels": POD_SECURITY_PRIVILEGED_LABELS,
            },
            opts=child_options(
                provider=workload_provider,
                depends_on=[prometheus],
                retain_on_delete=True,
            ),
        )
        slurm_operator_crds = k8s.helm.v3.Release(
            "slurm-operator-crds",
            chart=_SLINKY_OPERATOR_CRDS_CHART,
            version=_SLINKY_CHART_VERSION,
            namespace=_SLINKY_OPERATOR_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            opts=child_options(
                provider=workload_provider,
                depends_on=[slinky_namespace, prometheus],
                retain_on_delete=True,
            ),
        )
        slurm_operator = k8s.helm.v3.Release(
            "slurm-operator",
            chart=_SLINKY_OPERATOR_CHART,
            version=_SLINKY_CHART_VERSION,
            namespace=_SLINKY_OPERATOR_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values=_slurm_operator_values(),
            opts=child_options(
                provider=workload_provider,
                depends_on=[slinky_namespace, cert_manager, slurm_operator_crds],
                retain_on_delete=True,
            ),
        )
        slurm_release = k8s.helm.v3.Release(
            "slurm",
            chart=_SLURM_CHART,
            version=_SLINKY_CHART_VERSION,
            namespace=_SLURM_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=900,
            values=_slurm_values(slurm_node_sets),
            opts=child_options(
                provider=workload_provider,
                depends_on=[
                    slurm_namespace,
                    slurm_operator,
                ],
                retain_on_delete=True,
            ),
        )
        keda: KEDANodeSetScaler | None = None
        if keda_scaled_node_sets:
            keda = KEDANodeSetScaler(
                "keda-nodeset-scaler",
                instance=instance,
                prometheus_release_name=prometheus.status.name,
                slurm_release_name=slurm_release.status.name,
                scaled_node_sets=keda_scaled_node_sets,
                provider=workload_provider,
                depends_on=[prometheus, slurm_release],
                opts=child_options(provider=workload_provider),
            )

        self.keda_namespace = pulumi.Output.from_input(
            keda.namespace if keda is not None else None
        )
        self.keda_scaled_object_names = pulumi.Output.from_input(
            keda.scaled_object_names if keda is not None else []
        )
        self.keda_status = pulumi.Output.from_input(
            keda.status if keda is not None else None
        )
        self.prometheus_namespace = pulumi.Output.from_input(_PROMETHEUS_NAMESPACE)
        self.prometheus_status = prometheus.status
        self.slurm_operator_status = slurm_operator.status
        self.slurm_status = slurm_release.status
        self.workload_cluster_ready = pulumi.Output.all(
            self.keda_status,
            prometheus.status,
            slurm_operator.status,
            slurm_release.status,
        ).apply(lambda _: True)

        self.register_outputs(
            {
                "keda_chart_version": _KEDA_CHART_VERSION,
                "keda_namespace": self.keda_namespace,
                "keda_scaled_object_names": self.keda_scaled_object_names,
                "keda_status": self.keda_status,
                "prometheus_chart_version": _PROMETHEUS_CHART_VERSION,
                "prometheus_namespace": self.prometheus_namespace,
                "prometheus_status": self.prometheus_status,
                "workload_cluster_ready": self.workload_cluster_ready,
                "slurm_operator_chart_version": _SLINKY_CHART_VERSION,
                "slurm_operator_status": self.slurm_operator_status,
                "slurm_chart_version": _SLINKY_CHART_VERSION,
                "slurm_status": self.slurm_status,
            }
        )