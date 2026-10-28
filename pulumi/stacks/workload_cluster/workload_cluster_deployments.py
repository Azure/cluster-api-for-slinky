# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Common Kubernetes deployments installed onto workload clusters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pulumi
import pulumi_kubernetes as k8s
from pydantic import BaseModel, ConfigDict, StrictBool, model_validator

from lib.config import (
    NonEmptyStr,
    PulumiConfigModel,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    POD_SECURITY_PRIVILEGED_LABELS,
    controller_node_affinity,
    controller_pod_spec,
    controller_tolerations,
    node_type_affinity,
)
from stacks.kubernetes_annotations import pulumi_patch_force, pulumi_wait_for


_CERT_MANAGER_CHART_REPO = "https://charts.jetstack.io"
_CERT_MANAGER_CHART_NAME = "cert-manager"
_CERT_MANAGER_CHART_VERSION = "v1.21.3"
_CERT_MANAGER_NAMESPACE = "cert-manager"

_PROMETHEUS_CHART_REPO = "https://prometheus-community.github.io/helm-charts"
_PROMETHEUS_CHART_NAME = "kube-prometheus-stack"
_PROMETHEUS_CHART_VERSION = "92.14.1"
_PROMETHEUS_NAMESPACE = "prometheus"

_KEDA_CHART_REPO = "https://kedacore.github.io/charts"
_KEDA_CHART_NAME = "keda"
_KEDA_CHART_VERSION = "2.21.0"
_KEDA_SCALED_OBJECT_API_VERSION = "keda.sh/v1alpha1"
_SLURM_NODESET_API_VERSION = "slinky.slurm.net/v1beta1"
_PROMETHEUS_PORT = 9090
_SLURM_CAPACITY_NAME = "slurm-capacity-metrics"
_SLURM_CAPACITY_PORT = 9091
_SLURM_CAPACITY_IMAGE = "ghcr.io/slinkyproject/slurmctld:26.05-ubuntu26.04"

_SLINKY_CHART_OCI_PREFIX = "oci://ghcr.io/slinkyproject/charts"
_SLINKY_CHART_VERSION = "1.2.3"
_SLINKY_OPERATOR_NAMESPACE = "slinky"
_SLURM_NAMESPACE = "slurm"
_SLURM_BRIDGE_CHART = "oci://ghcr.io/slinkyproject/charts/slurm-bridge"
_SLURM_BRIDGE_CHART_VERSION = "1.2.3"
_SLURM_BRIDGE_NAMESPACE = "slurm-bridge"
_SLURM_BRIDGE_PARTITION = "compute"
_SLURM_BRIDGE_SCHEDULER_NAME = "slurm-bridge-scheduler"
_SLURM_BRIDGE_TOKEN_NAME = "slurm-bridge-token"
_SLURM_BRIDGE_TOKEN_API_VERSION = "slinky.slurm.net/v1beta1"


class SlinkyImageConfig(PulumiConfigModel):
    repository: NonEmptyStr
    tag: NonEmptyStr | None = None
    digest: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> SlinkyImageConfig:
        if (self.tag is None) == (self.digest is None):
            raise ValueError("exactly one of tag or digest is required")
        return self

    def to_helm_values(self) -> dict[str, str]:
        values = {"repository": self.repository}
        if self.tag is not None:
            values["tag"] = self.tag
        if self.digest is not None:
            values["digest"] = self.digest
        return values


class SlinkyDeploymentConfig(PulumiConfigModel):
    chart_oci_prefix: NonEmptyStr = _SLINKY_CHART_OCI_PREFIX
    chart_plain_http: StrictBool = False
    operator_crds_chart_version: NonEmptyStr = _SLINKY_CHART_VERSION
    operator_chart_version: NonEmptyStr = _SLINKY_CHART_VERSION
    slurm_chart_version: NonEmptyStr = _SLINKY_CHART_VERSION
    operator_image: SlinkyImageConfig | None = None
    webhook_image: SlinkyImageConfig | None = None
    image_pull_secrets: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_chart_source(self) -> SlinkyDeploymentConfig:
        if self.chart_plain_http and not self.chart_oci_prefix.startswith("oci://"):
            raise ValueError(
                "chartPlainHttp requires chartOciPrefix to use the oci:// scheme"
            )
        return self

    def chart(self, name: str) -> str:
        return f"{self.chart_oci_prefix.rstrip('/')}/{name}"


class SlurmNodeSetSpec(PulumiConfigModel):
    name: NonEmptyStr
    node_type: NonEmptyStr
    replicas: StrictPositiveInt


class KEDANodeSetScalerSpec(PulumiConfigModel):
    node_set_name: NonEmptyStr
    min_replicas: StrictNonNegativeInt
    max_replicas: StrictPositiveInt


class KEDAOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    chart_version: str
    namespace: str
    release_name: str
    scaled_object_names: list[str]
    status: Any


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
    normalized = normalized[: dns_label_max_length - len(suffix) - 1].rstrip("-")
    return f"{normalized}-{suffix}"


def _slurm_nodeset_name(slurm_release_name: str, worker_name: str) -> str:
    return f"{slurm_release_name}-worker-{worker_name}"


def _slurm_bridge_toleration() -> dict[str, str]:
    return {
        "key": "slinky.slurm.net/managed-node",
        "operator": "Equal",
        "value": _SLURM_BRIDGE_SCHEDULER_NAME,
        "effect": "NoExecute",
    }


def _slurm_bridge_values() -> dict[str, object]:
    placement = controller_pod_spec()
    return {
        "schedulerConfig": {"partition": _SLURM_BRIDGE_PARTITION},
        "sharedConfig": {"slurmJwtSecret": _SLURM_BRIDGE_TOKEN_NAME},
        "admission": placement,
        "controllers": placement,
        "scheduler": placement,
    }


def _slurm_bridge_token_spec() -> dict[str, object]:
    return {
        "jwtKeyRef": {"name": "slurm-auth-jwt", "key": "jwt.key"},
        "secretRef": {"name": _SLURM_BRIDGE_TOKEN_NAME, "key": "auth-token"},
        "username": "slurm",
        "refresh": True,
        "lifetime": "8760h",
    }


def _prometheus_service_name(prometheus_release_name: str) -> str:
    return f"{prometheus_release_name}-kube-p-prometheus"


def _prometheus_server_address(prometheus_release_name: str) -> str:
    return (
        f"http://{_prometheus_service_name(prometheus_release_name)}."
        f"{_PROMETHEUS_NAMESPACE}.svc.cluster.local:{_PROMETHEUS_PORT}"
    )


def _keda_values() -> dict[str, object]:
    component_placement = {
        "affinity": controller_node_affinity(),
        "tolerations": controller_tolerations(),
    }
    return {
        **component_placement,
        "metricsServer": component_placement,
        "webhooks": component_placement,
    }


def _keda_scaled_object_spec(
    *,
    node_set_name: pulumi.Input[str],
    partition_name: str,
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
                    "query": (
                        "max(ca4s_slurm_worker_demand"
                        f"{{namespace={json.dumps(_SLURM_NAMESPACE)},"
                        f"partition={json.dumps(partition_name)}}} "
                        "and on (job, instance) "
                        f'(up{{namespace="{_SLURM_NAMESPACE}",'
                        f'service="{_SLURM_CAPACITY_NAME}"}} == 1))'
                    ),
                    "threshold": "1",
                    "ignoreNullValues": "false",
                    "unsafeSsl": "true",
                },
            }
        ],
    }


def _slurm_operator_values(
    config: SlinkyDeploymentConfig | None = None,
) -> dict[str, object]:
    config = config or SlinkyDeploymentConfig()
    values: dict[str, object] = {
        "operator": {
            **controller_pod_spec(),
        },
        "webhook": {
            **controller_pod_spec(),
            "podsBinding": True,
            "mutating": {"failurePolicy": "Fail"},
        },
    }
    if config.operator_image is not None:
        values["operator"]["image"] = config.operator_image.to_helm_values()
    if config.webhook_image is not None:
        values["webhook"]["image"] = config.webhook_image.to_helm_values()
    if config.image_pull_secrets:
        values["imagePullSecrets"] = [
            {"name": name} for name in config.image_pull_secrets
        ]
    return values


def _cert_manager_values() -> dict[str, object]:
    return {
        "crds": {"enabled": True},
        **controller_pod_spec(),
        "webhook": controller_pod_spec(),
        "cainjector": controller_pod_spec(),
        "startupapicheck": controller_pod_spec(),
    }


def _prometheus_values() -> dict[str, object]:
    affinity = controller_node_affinity()
    tolerations = controller_tolerations()
    return {
        "prometheus": {
            "prometheusSpec": {
                "serviceMonitorSelectorNilUsesHelmValues": False,
                "podMonitorSelectorNilUsesHelmValues": False,
                "affinity": affinity,
                "tolerations": tolerations,
            },
        },
        "alertmanager": {
            "alertmanagerSpec": {
                "affinity": affinity,
                "tolerations": tolerations,
            },
        },
        "prometheusOperator": {
            "affinity": affinity,
            "tolerations": tolerations,
            "admissionWebhooks": {
                "patch": {
                    "affinity": affinity,
                    "tolerations": tolerations,
                },
            },
        },
        "grafana": {
            "affinity": affinity,
            "tolerations": tolerations,
        },
        "kube-state-metrics": {
            "affinity": affinity,
            "tolerations": tolerations,
        },
    }


def _coredns_controller_placement_spec() -> dict[str, object]:
    return {"template": {"spec": controller_pod_spec()}}


def _slurm_nodeset_values(node_set: SlurmNodeSetSpec) -> dict[str, object]:
    return {
        "enabled": True,
        "replicas": node_set.replicas,
        "slurmd": {
            "image": {
                "repository": "ghcr.io/slinkyproject/slurmd",
                "tag": "26.05-ubuntu24.04",
            },
            "args": [],
            "resources": {},
            "volumeMounts": [],
        },
        "logfile": {
            "image": {
                "repository": "public.ecr.aws/docker/library/alpine",
                "tag": "3.24.3",
            },
        },
        "partition": {"enabled": True, "configMap": {}},
        "useResourceLimits": True,
        "updateStrategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxUnavailable": "25%"},
        },
        "pinToNode": True,
        "preferKubernetesNodeName": True,
        "pruneSlurmNodeRecords": "Auto",
        "oversubscribeNode": False,
        "podSpec": {
            "affinity": node_type_affinity(node_set.node_type),
            "tolerations": [_slurm_bridge_toleration()],
        },
    }


def _slurm_values(
    node_sets: tuple[SlurmNodeSetSpec, ...],
    scaled_node_sets: tuple[KEDANodeSetScalerSpec, ...] = (),
) -> dict[str, object]:
    values = {
        "configFiles": {
            "cgroup.conf": "CgroupPlugin=cgroup/v2\nIgnoreSystemd=yes\n",
        },
        "controller": {
            "extraConfMap": {"ReturnToService": "2"},
            "logfile": {
                "image": {
                    "repository": "public.ecr.aws/docker/library/alpine",
                    "tag": "3.24.3",
                },
            },
            "metrics": {
                "enabled": True,
                "serviceMonitor": {"enabled": True},
            },
            "podSpec": controller_pod_spec(),
        },
        "loginsets": {
            "slinky": {
                "enabled": True,
                "initconf": {
                    "image": {
                        "repository": "public.ecr.aws/docker/library/alpine",
                        "tag": "3.24.3",
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
                "podSpec": controller_pod_spec(),
            },
        },
        "nodesets": {
            "slinky": {"enabled": False},
            **{
                node_set.name: _slurm_nodeset_values(node_set)
                for node_set in node_sets
            },
        },
        "restapi": {"podSpec": controller_pod_spec()},
    }
    if scaled_node_sets:
        script = Path(__file__).with_name("slurm_capacity.py").read_text()
        limits = json.dumps({
            scaler.node_set_name: scaler.max_replicas for scaler in scaled_node_sets
        }, sort_keys=True)
        name = _SLURM_CAPACITY_NAME
        label = {"ca4s.azure.com/slurm-capacity": "enabled"}
        values["controller"]["metadata"] = {
            "labels": label,
            "annotations": {
                "checksum/ca4s-slurm-capacity": hashlib.sha256(
                    (script + limits).encode()
                ).hexdigest(),
            },
        }
        values["controller"]["podSpec"].update({
            "containers": [{
                "name": name,
                "image": _SLURM_CAPACITY_IMAGE,
                "command": ["python3", "/opt/ca4s/slurm_capacity.py", "--limits", limits],
                "ports": [{"name": "capacity", "containerPort": _SLURM_CAPACITY_PORT}],
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
                "volumeMounts": [
                    {"name": name, "mountPath": "/opt/ca4s", "readOnly": True},
                    {"name": "slurm-etc", "mountPath": "/etc/slurm", "readOnly": True},
                    {"name": "slurm-authsocket", "mountPath": "/run/slurmctld", "readOnly": True},
                ],
                "readinessProbe": {
                    "httpGet": {"path": "/healthz", "port": "capacity"},
                },
                "livenessProbe": {
                    "tcpSocket": {"port": "capacity"},
                    "timeoutSeconds": 5,
                    "periodSeconds": 30,
                },
            }],
            "volumes": [{"name": name, "configMap": {"name": name}}],
        })
        values["extraObjects"] = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "annotations": pulumi_patch_force()},
                "data": {"slurm_capacity.py": script},
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "labels": label},
                "spec": {
                    "selector": label,
                    "ports": [{"name": "capacity", "port": _SLURM_CAPACITY_PORT}],
                },
            },
            {
                "apiVersion": "monitoring.coreos.com/v1",
                "kind": "ServiceMonitor",
                "metadata": {"name": name},
                "spec": {
                    "selector": {"matchLabels": label},
                    "endpoints": [{
                        "port": "capacity",
                        "interval": "30s",
                        "scrapeTimeout": "25s",
                    }],
                },
            },
        ]
    return values


class KEDANodeSetScaler(pulumi.ComponentResource):
    """Translate occupied workers plus eligible queued capacity into replicas."""

    status: pulumi.Output[Any]
    outputs: pulumi.Output[KEDAOutputs]

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
                    partition_name=scaled_node_set.node_set_name,
                    min_replicas=scaled_node_set.min_replicas,
                    max_replicas=scaled_node_set.max_replicas,
                    prometheus_server_address=prometheus_server_address,
                ),
                opts=child_options(depends_on=[release, *(depends_on or [])]),
            )

        self.status = release.status
        outputs = {
            "chart_version": pulumi.Output.from_input(_KEDA_CHART_VERSION),
            "namespace": pulumi.Output.from_input(namespace_name),
            "release_name": pulumi.Output.from_input(release_name),
            "scaled_object_names": pulumi.Output.from_input(scaled_object_names),
            "status": self.status,
        }
        self.outputs = pulumi.Output.all(**outputs).apply(KEDAOutputs.model_validate)
        self.register_outputs(outputs)


class WorkloadClusterDeployments(pulumi.ComponentResource):
    """Kubernetes deployments installed after a workload cluster exists."""

    keda: pulumi.Output[KEDAOutputs] | None
    prometheus_namespace: pulumi.Output[str]
    prometheus_status: pulumi.Output[Any]
    slurm_bridge_chart_version: pulumi.Output[str]
    slurm_operator_status: pulumi.Output[Any]
    slurm_status: pulumi.Output[Any]
    slurm_bridge_status: pulumi.Output[Any]
    workload_cluster_ready: pulumi.Output[bool]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        slurm_node_sets: tuple[SlurmNodeSetSpec, ...],
        keda_scaled_node_sets: tuple[KEDANodeSetScalerSpec, ...] = (),
        slinky: SlinkyDeploymentConfig | None = None,
        workload_provider: k8s.Provider,
        pin_coredns_to_controller: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:WorkloadClusterDeployments",
            name,
            props={},
            opts=opts,
        )
        slinky = slinky or SlinkyDeploymentConfig()
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

        if pin_coredns_to_controller:
            k8s.apps.v1.DeploymentPatch(
                "coredns-controller-placement",
                metadata={
                    "name": "coredns",
                    "namespace": "kube-system",
                    "annotations": pulumi_patch_force(),
                },
                spec=_coredns_controller_placement_spec(),
                opts=child_options(provider=workload_provider),
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
            values=_cert_manager_values(),
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
        slurm_bridge_namespace = k8s.core.v1.Namespace(
            "slurm-bridge-namespace",
            metadata={"name": _SLURM_BRIDGE_NAMESPACE},
            opts=child_options(
                provider=workload_provider,
                retain_on_delete=True,
            ),
        )
        # TODO: Unify on Helm v4 once Pulumi ships a production Release resource
        # with plain-HTTP OCI support, atomic cleanup, job waits, timeouts, and
        # release status. The current v4 Chart only renders and manages manifests.
        if slinky.chart_plain_http:
            slurm_operator_crds = k8s.helm.v4.Chart(
                "slurm-operator-crds",
                chart=slinky.chart("slurm-operator-crds"),
                name="slurm-operator-crds",
                version=slinky.operator_crds_chart_version,
                namespace=_SLINKY_OPERATOR_NAMESPACE,
                plain_http=True,
                opts=child_options(
                    provider=workload_provider,
                    depends_on=[slinky_namespace, prometheus],
                    retain_on_delete=True,
                ),
            )
            slurm_operator = k8s.helm.v4.Chart(
                "slurm-operator",
                chart=slinky.chart("slurm-operator"),
                name="slurm-operator",
                version=slinky.operator_chart_version,
                namespace=_SLINKY_OPERATOR_NAMESPACE,
                plain_http=True,
                values=_slurm_operator_values(slinky),
                opts=child_options(
                    provider=workload_provider,
                    depends_on=[slinky_namespace, cert_manager, slurm_operator_crds],
                    retain_on_delete=True,
                ),
            )
            slurm_release_name: pulumi.Input[str] = "slurm"
            slurm_release = k8s.helm.v4.Chart(
                "slurm",
                chart=slinky.chart("slurm"),
                name=slurm_release_name,
                version=slinky.slurm_chart_version,
                namespace=_SLURM_NAMESPACE,
                plain_http=True,
                values=_slurm_values(slurm_node_sets, keda_scaled_node_sets),
                opts=child_options(
                    provider=workload_provider,
                    depends_on=[slurm_namespace, slurm_operator],
                    retain_on_delete=True,
                ),
            )
            slurm_operator_status = slurm_operator.resources.apply(
                lambda _: {
                    "chart": "slurm-operator",
                    "name": "slurm-operator",
                    "namespace": _SLINKY_OPERATOR_NAMESPACE,
                    "status": "deployed",
                    "version": slinky.operator_chart_version,
                }
            )
            slurm_status = slurm_release.resources.apply(
                lambda _: {
                    "chart": "slurm",
                    "name": "slurm",
                    "namespace": _SLURM_NAMESPACE,
                    "status": "deployed",
                    "version": slinky.slurm_chart_version,
                }
            )
            slurm_ready = slurm_release.resources
        else:
            slurm_operator_crds = k8s.helm.v3.Release(
                "slurm-operator-crds",
                chart=slinky.chart("slurm-operator-crds"),
                version=slinky.operator_crds_chart_version,
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
                chart=slinky.chart("slurm-operator"),
                version=slinky.operator_chart_version,
                namespace=_SLINKY_OPERATOR_NAMESPACE,
                cleanup_on_fail=True,
                atomic=True,
                wait_for_jobs=True,
                timeout=600,
                values=_slurm_operator_values(slinky),
                opts=child_options(
                    provider=workload_provider,
                    depends_on=[slinky_namespace, cert_manager, slurm_operator_crds],
                    retain_on_delete=True,
                ),
            )
            slurm_release = k8s.helm.v3.Release(
                "slurm",
                chart=slinky.chart("slurm"),
                version=slinky.slurm_chart_version,
                namespace=_SLURM_NAMESPACE,
                cleanup_on_fail=True,
                atomic=True,
                wait_for_jobs=True,
                timeout=900,
                values=_slurm_values(slurm_node_sets, keda_scaled_node_sets),
                opts=child_options(
                    provider=workload_provider,
                    depends_on=[slurm_namespace, slurm_operator],
                    retain_on_delete=True,
                ),
            )
            slurm_release_name = slurm_release.status.name
            slurm_operator_status = slurm_operator.status
            slurm_status = slurm_release.status
            slurm_ready = slurm_release.status

        slurm_bridge_token = k8s.apiextensions.CustomResource(
            "slurm-bridge-token",
            api_version=_SLURM_BRIDGE_TOKEN_API_VERSION,
            kind="Token",
            metadata={
                "name": _SLURM_BRIDGE_TOKEN_NAME,
                "namespace": _SLURM_NAMESPACE,
                "annotations": pulumi_wait_for("jsonpath={.status.issuedAt}"),
            },
            spec=_slurm_bridge_token_spec(),
            opts=child_options(
                provider=workload_provider,
                depends_on=[slurm_release, slurm_operator],
                retain_on_delete=True,
            ),
        )
        slurm_bridge = k8s.helm.v3.Release(
            "slurm-bridge",
            chart=_SLURM_BRIDGE_CHART,
            name="slurm-bridge",
            version=_SLURM_BRIDGE_CHART_VERSION,
            namespace=_SLURM_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            values=_slurm_bridge_values(),
            opts=child_options(
                provider=workload_provider,
                depends_on=[
                    slurm_bridge_namespace,
                    slurm_bridge_token,
                    slurm_release,
                    cert_manager,
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
                slurm_release_name=slurm_release_name,
                scaled_node_sets=keda_scaled_node_sets,
                provider=workload_provider,
                depends_on=[prometheus, slurm_release],
                opts=child_options(provider=workload_provider),
            )

        self.keda = keda.outputs if keda is not None else None
        self.prometheus_namespace = pulumi.Output.from_input(_PROMETHEUS_NAMESPACE)
        self.prometheus_status = prometheus.status
        self.slurm_operator_status = slurm_operator_status
        self.slurm_status = slurm_status
        self.slurm_bridge_chart_version = pulumi.Output.from_input(
            _SLURM_BRIDGE_CHART_VERSION
        )
        self.slurm_bridge_status = slurm_bridge.status
        ready_inputs: list[pulumi.Input[Any]] = [
            prometheus.status,
            slurm_operator_status,
            slurm_ready,
            slurm_bridge.status,
        ]
        if keda is not None:
            ready_inputs.append(keda.status)
        self.workload_cluster_ready = pulumi.Output.all(*ready_inputs).apply(lambda _: True)

        self.register_outputs(
            {
                "keda": (
                    self.keda.apply(lambda outputs: outputs.model_dump())
                    if self.keda is not None
                    else None
                ),
                "prometheus_chart_version": _PROMETHEUS_CHART_VERSION,
                "prometheus_namespace": self.prometheus_namespace,
                "prometheus_status": self.prometheus_status,
                "workload_cluster_ready": self.workload_cluster_ready,
                "slurm_operator_chart_version": slinky.operator_chart_version,
                "slurm_operator_status": self.slurm_operator_status,
                "slurm_chart_version": slinky.slurm_chart_version,
                "slurm_status": self.slurm_status,
                "slurm_bridge_chart_version": self.slurm_bridge_chart_version,
                "slurm_bridge_status": self.slurm_bridge_status,
            }
        )