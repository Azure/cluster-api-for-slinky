"""AKS workload-cluster class composition."""

from __future__ import annotations

from typing import Any, Literal, Mapping
from uuid import UUID

import pulumi
from pydantic import Field, field_serializer

from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from localenv import discover_azure_resource_placement, discover_local_username

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

_DEFAULT_AKS_INSTANCE_NAME = "caps-aks"
_DEFAULT_AKS_KUBERNETES_VERSION = "v1.33.12"
_DEFAULT_AKS_NODE_SKU = "Standard_D2as_v5"
_DEFAULT_AKS_NODE_COUNT = 1


class AKSWorkloadSizingConfig(PulumiConfigModel):
    """Optional AKS version and node-pool sizing overrides."""

    kubernetes_version: NonEmptyStr = _DEFAULT_AKS_KUBERNETES_VERSION
    node_sku: NonEmptyStr = _DEFAULT_AKS_NODE_SKU
    node_count: StrictPositiveInt = _DEFAULT_AKS_NODE_COUNT


class AzureWorkloadSpec(PulumiConfigModel):
    """AKS placement and sizing parameters for one workload-cluster entry.

    ``subscription_id``, ``location``, and ``resource_group`` may be omitted
    from config because they default from local Azure resource placement discovery.
    """

    subscription_id: UUID = Field(
        default_factory=lambda: UUID(
            discover_azure_resource_placement(raise_on_missing=True).subscription_id
        )
    )
    location: NonEmptyStr = Field(
        default_factory=lambda: (
            discover_azure_resource_placement(raise_on_missing=True).location
        )
    )
    resource_group: NonEmptyStr = Field(
        default_factory=lambda: (
            discover_azure_resource_placement(raise_on_missing=True).resource_group
        )
    )
    additional_tags: Mapping[NonEmptyStr, str] = Field(
        default_factory=lambda: (
            {"Owner": username}
            if (username := discover_local_username()) is not None
            else {}
        )
    )
    aks: AKSWorkloadSizingConfig = AKSWorkloadSizingConfig()

    @field_serializer("subscription_id", "location", "resource_group", check_fields=False)
    def serialize_placement(self, value: UUID | str) -> str:
        return str(value)

    @field_serializer("additional_tags", check_fields=False)
    def serialize_additional_tags(
        self,
        additional_tags: Mapping[NonEmptyStr, str],
    ) -> dict[str, str]:
        return dict(additional_tags)


class AKSWorkloadClusterConfig(PulumiConfigModel):
    class_name: Literal["aks"] = _CLUSTER_CLASS
    parameters: AzureWorkloadSpec

    @field_serializer("class_name")
    def serialize_class_name(self, class_name: str) -> str:
        return class_name


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
        config: AKSWorkloadClusterConfig,
        context: Any | None = None,
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
        workload_spec = config.parameters
        if context is not None:
            identity_name = identity_name or context.identity_name
            identity_namespace = identity_namespace or context.identity_namespace
        if identity_name is None:
            raise ValueError("aks workload cluster class requires identity_name")
        if identity_namespace is None:
            raise ValueError("aks workload cluster class requires identity_namespace")
        location = workload_spec.location
        resource_group = workload_spec.resource_group

        def child_options(
            *,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                depends_on=depends_on,
            )

        node_pools = node_pools or _default_aks_node_pools(workload_spec.aks.node_count)

        infrastructure = AKSWorkloadClusterInfrastructure(
            "infrastructure",
            instance=instance,
            subscription_id=str(workload_spec.subscription_id),
            identity_name=identity_name,
            identity_namespace=identity_namespace,
            location=location,
            resource_group=resource_group,
            kubernetes_version=workload_spec.aks.kubernetes_version,
            node_sku=workload_spec.aks.node_sku,
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
