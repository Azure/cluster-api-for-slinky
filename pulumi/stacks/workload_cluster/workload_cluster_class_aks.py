"""AKS workload-cluster class composition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import pulumi

from stacks.workload_cluster.workload_cluster_deployments import (
    KEDAOutputs,
    KEDANodeSetScalerSpec,
    SlurmNodeSetSpec,
    _PROMETHEUS_CHART_VERSION,
    _SLINKY_CHART_VERSION,
    WorkloadClusterDeployments,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    COMPUTE_NODE_TYPE,
    CONTROLLER_NODE_TYPE,
)
from stacks.workload_cluster.workload_cluster_infrastructure_aks import (
    AKSNodePoolSpec,
    AKSWorkloadClusterInfrastructure,
)


_CLUSTER_CLASS = "aks"
_TENANTS_SPEC_CONFIG_KEY = "spec"
_CONFIG_WORKLOAD_CLUSTERS = "workloadClusters"
_CONFIG_METADATA = "metadata"
_CONFIG_NAME = "name"
_CONFIG_CLASS_NAME = "className"
_CONFIG_PARAMETERS = "parameters"
_CONFIG_LOCATION = "location"
_CONFIG_RESOURCE_GROUP = "resourceGroup"
_CONFIG_ADDITIONAL_TAGS = "additionalTags"
_CONFIG_AKS = "aks"
_CONFIG_AKS_KUBERNETES_VERSION = "kubernetesVersion"
_CONFIG_AKS_NODE_SKU = "nodeSku"
_CONFIG_AKS_NODE_COUNT = "nodeCount"

_DEFAULT_AKS_INSTANCE_NAME = "caps-aks"
_DEFAULT_AKS_KUBERNETES_VERSION = "v1.33.12"
_DEFAULT_AKS_NODE_SKU = "Standard_D2as_v5"
_DEFAULT_AKS_NODE_COUNT = 1


@dataclass(frozen=True)
class AzureWorkloadSpec:
    """AKS placement and sizing parameters for one workload-cluster entry."""

    location: str
    resource_group: str
    additional_tags: Mapping[str, str] = field(default_factory=dict)
    kubernetes_version: str = _DEFAULT_AKS_KUBERNETES_VERSION
    node_sku: str = _DEFAULT_AKS_NODE_SKU
    node_count: int = _DEFAULT_AKS_NODE_COUNT


def _require_mapping(field_path: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object")
    return value


def _require_non_empty_str(field_path: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_path} must be a non-empty string")
    return value


def _require_positive_int(field_path: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_path} must be an integer >= 1")
    return value


def _require_str_map(field_path: str, value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object of strings")
    parsed: dict[str, str] = {}
    for key, item_value in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_path} keys must be non-empty strings")
        if not isinstance(item_value, str):
            raise ValueError(f"{field_path}.{key} must be a string")
        parsed[key] = item_value
    return parsed


def parse_azure_workload_spec(
    value: object | None,
    *,
    field_path: str = f"{_TENANTS_SPEC_CONFIG_KEY}.{_CONFIG_PARAMETERS}",
) -> AzureWorkloadSpec:
    """Parse AKS class parameters from one workload-cluster entry."""
    if value is None:
        raise ValueError(f"{field_path} must be an object")
    parameters = _require_mapping(field_path, value)

    location = _require_non_empty_str(
        f"{field_path}.{_CONFIG_LOCATION}",
        parameters.get(_CONFIG_LOCATION),
    )
    resource_group = _require_non_empty_str(
        f"{field_path}.{_CONFIG_RESOURCE_GROUP}",
        parameters.get(_CONFIG_RESOURCE_GROUP),
    )

    tags_value = parameters.get(_CONFIG_ADDITIONAL_TAGS)
    additional_tags = (
        {}
        if tags_value is None
        else _require_str_map(f"{field_path}.{_CONFIG_ADDITIONAL_TAGS}", tags_value)
    )

    aks_value = parameters.get(_CONFIG_AKS)
    if aks_value is None:
        aks_fields: Mapping[str, object] = {}
    else:
        aks_fields = _require_mapping(f"{field_path}.{_CONFIG_AKS}", aks_value)

    aks_path = f"{field_path}.{_CONFIG_AKS}"
    version_value = aks_fields.get(_CONFIG_AKS_KUBERNETES_VERSION)
    kubernetes_version = (
        _DEFAULT_AKS_KUBERNETES_VERSION
        if version_value is None
        else _require_non_empty_str(
            f"{aks_path}.{_CONFIG_AKS_KUBERNETES_VERSION}", version_value
        )
    )
    sku_value = aks_fields.get(_CONFIG_AKS_NODE_SKU)
    node_sku = (
        _DEFAULT_AKS_NODE_SKU
        if sku_value is None
        else _require_non_empty_str(f"{aks_path}.{_CONFIG_AKS_NODE_SKU}", sku_value)
    )
    count_value = aks_fields.get(_CONFIG_AKS_NODE_COUNT)
    node_count = (
        _DEFAULT_AKS_NODE_COUNT
        if count_value is None
        else _require_positive_int(f"{aks_path}.{_CONFIG_AKS_NODE_COUNT}", count_value)
    )

    return AzureWorkloadSpec(
        location=location,
        resource_group=resource_group,
        additional_tags=additional_tags,
        kubernetes_version=kubernetes_version,
        node_sku=node_sku,
        node_count=node_count,
    )


def _aks_parameters(
    *,
    location: str,
    resource_group: str,
    additional_tags: Mapping[str, str] | None = None,
    aks_kubernetes_version: str | None = None,
    aks_node_sku: str | None = None,
    aks_node_count: int | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {
        _CONFIG_LOCATION: location,
        _CONFIG_RESOURCE_GROUP: resource_group,
    }
    if additional_tags:
        parameters[_CONFIG_ADDITIONAL_TAGS] = dict(additional_tags)
    aks: dict[str, object] = {}
    if aks_kubernetes_version is not None:
        aks[_CONFIG_AKS_KUBERNETES_VERSION] = aks_kubernetes_version
    if aks_node_sku is not None:
        aks[_CONFIG_AKS_NODE_SKU] = aks_node_sku
    if aks_node_count is not None:
        aks[_CONFIG_AKS_NODE_COUNT] = aks_node_count
    if aks:
        parameters[_CONFIG_AKS] = aks
    return parameters


def build_aks_workload_cluster_child_config(
    *,
    location: str,
    resource_group: str,
    name: str = _DEFAULT_AKS_INSTANCE_NAME,
    additional_tags: Mapping[str, str] | None = None,
    aks_kubernetes_version: str | None = None,
    aks_node_sku: str | None = None,
    aks_node_count: int | None = None,
) -> dict[str, object]:
    """Build ``childConfig.spec`` for the default AKS workload-cluster entry."""
    return {
        _TENANTS_SPEC_CONFIG_KEY: {
            _CONFIG_WORKLOAD_CLUSTERS: [
                {
                    _CONFIG_METADATA: {_CONFIG_NAME: name},
                    _TENANTS_SPEC_CONFIG_KEY: {
                        _CONFIG_CLASS_NAME: _CLUSTER_CLASS,
                        _CONFIG_PARAMETERS: _aks_parameters(
                            location=location,
                            resource_group=resource_group,
                            additional_tags=additional_tags,
                            aks_kubernetes_version=aks_kubernetes_version,
                            aks_node_sku=aks_node_sku,
                            aks_node_count=aks_node_count,
                        ),
                    },
                }
            ]
        }
    }


def _default_aks_node_pools(node_count: int) -> tuple[AKSNodePoolSpec, ...]:
    return (
        AKSNodePoolSpec(
            name="head",
            node_type=CONTROLLER_NODE_TYPE,
            replicas=node_count,
            controller=True,
        ),
        AKSNodePoolSpec(
            name="compute",
            node_type=COMPUTE_NODE_TYPE,
            replicas=1,
            autoscaling_bounds=(1, 10),
        ),
    )


_AKS_SLURM_NODE_SETS = (
    SlurmNodeSetSpec(name="compute", node_type=COMPUTE_NODE_TYPE, replicas=1),
)
_AKS_KEDA_SCALED_NODE_SETS = (
    KEDANodeSetScalerSpec(node_set_name="compute", min_replicas=1, max_replicas=10),
)


class AKSWorkloadClusterClass(pulumi.ComponentResource):
    """Reusable AKS workload-cluster class."""

    cluster_class: pulumi.Output[str]
    cluster_instance: pulumi.Output[str]
    cluster_name: pulumi.Output[str]
    control_plane_name: pulumi.Output[str]
    machine_pool_name: pulumi.Output[str]
    machine_pool_names: list[pulumi.Output[str]]
    control_plane_ready: pulumi.Output[bool]
    keda: KEDAOutputs | None
    prometheus_namespace: pulumi.Output[str]
    prometheus_status: pulumi.Output[Any]
    workload_cluster_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        parameters: Mapping[str, object] | None = None,
        context: Any | None = None,
        workload_spec: Any | None = None,
        subscription_id: str | None = None,
        identity_name: pulumi.Input[str] | None = None,
        identity_namespace: pulumi.Input[str] | None = None,
        node_pools: tuple[AKSNodePoolSpec, ...] | None = None,
        slurm_node_sets: tuple[SlurmNodeSetSpec, ...] = _AKS_SLURM_NODE_SETS,
        keda_scaled_node_sets: tuple[KEDANodeSetScalerSpec, ...] = _AKS_KEDA_SCALED_NODE_SETS,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:AKSWorkloadClusterClass",
            name,
            props={},
            opts=opts,
        )
        if workload_spec is None:
            workload_spec = parse_azure_workload_spec(parameters)
        if context is not None:
            subscription_id = subscription_id or context.subscription_id
            identity_name = identity_name or context.identity_name
            identity_namespace = identity_namespace or context.identity_namespace
        if subscription_id is None:
            raise ValueError("aks workload cluster class requires subscription_id")
        if identity_name is None:
            raise ValueError("aks workload cluster class requires identity_name")
        if identity_namespace is None:
            raise ValueError("aks workload cluster class requires identity_namespace")

        def child_options(
            *,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                depends_on=depends_on,
            )

        node_pools = node_pools or _default_aks_node_pools(workload_spec.node_count)

        infrastructure = AKSWorkloadClusterInfrastructure(
            "infrastructure",
            instance=instance,
            subscription_id=subscription_id,
            identity_name=identity_name,
            identity_namespace=identity_namespace,
            location=workload_spec.location,
            resource_group=workload_spec.resource_group,
            kubernetes_version=workload_spec.kubernetes_version,
            node_sku=workload_spec.node_sku,
            node_pools=node_pools,
            additional_tags=workload_spec.additional_tags,
            opts=child_options(),
        )
        deployments = WorkloadClusterDeployments(
            "deployments",
            instance=instance,
            slurm_node_sets=slurm_node_sets,
            keda_scaled_node_sets=keda_scaled_node_sets,
            workload_provider=infrastructure.workload_provider,
            opts=child_options(depends_on=[infrastructure]),
        )

        self.cluster_class = pulumi.Output.from_input(_CLUSTER_CLASS)
        self.cluster_instance = pulumi.Output.from_input(instance)
        self.cluster_name = infrastructure.cluster_name
        self.control_plane_name = infrastructure.control_plane_name
        self.machine_pool_name = infrastructure.machine_pool_name
        self.machine_pool_names = infrastructure.machine_pool_names
        self.control_plane_ready = infrastructure.control_plane_ready
        self.keda = deployments.keda
        self.prometheus_namespace = deployments.prometheus_namespace
        self.prometheus_status = deployments.prometheus_status
        self.workload_cluster_ready = deployments.workload_cluster_ready
        self.todo = pulumi.Output.from_input(
            "Validate AKS workload-driven autoscaling end-to-end."
        )

        self.register_outputs(
            {
                "cluster_class": self.cluster_class,
                "cluster_instance": self.cluster_instance,
                "cluster_name": self.cluster_name,
                "control_plane_name": self.control_plane_name,
                "machine_pool_name": self.machine_pool_name,
                "machine_pool_names": self.machine_pool_names,
                "control_plane_ready": self.control_plane_ready,
                "keda": self.keda.to_outputs() if self.keda else None,
                "prometheus_chart_version": _PROMETHEUS_CHART_VERSION,
                "prometheus_namespace": self.prometheus_namespace,
                "prometheus_status": self.prometheus_status,
                "workload_cluster_ready": self.workload_cluster_ready,
                "slurm_operator_chart_version": _SLINKY_CHART_VERSION,
                "slurm_operator_status": deployments.slurm_operator_status,
                "slurm_chart_version": _SLINKY_CHART_VERSION,
                "slurm_status": deployments.slurm_status,
                "todo": self.todo,
            }
        )


WorkloadClusterClass = AKSWorkloadClusterClass
