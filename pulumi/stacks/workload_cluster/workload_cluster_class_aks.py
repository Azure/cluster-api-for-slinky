"""AKS workload-cluster class composition."""

from __future__ import annotations

from typing import Any

import pulumi

try:
    from .workload_cluster_infrastructure import (
        COMPUTE_NODE_TYPE,
        CONTROLLER_NODE_TYPE,
    )
    from .workload_cluster_deployments import (
        KEDANodeSetScalerSpec,
        SlurmNodeSetSpec,
        _KEDA_CHART_VERSION,
        _PROMETHEUS_CHART_VERSION,
        _SLINKY_CHART_VERSION,
        WorkloadClusterDeployments,
    )
    from .workload_cluster_infrastructure_aks import (
        AKSNodePoolSpec,
        AKSWorkloadClusterInfrastructure,
    )
except ImportError:
    from workload_cluster_infrastructure import (
        COMPUTE_NODE_TYPE,
        CONTROLLER_NODE_TYPE,
    )
    from workload_cluster_deployments import (
        KEDANodeSetScalerSpec,
        SlurmNodeSetSpec,
        _KEDA_CHART_VERSION,
        _PROMETHEUS_CHART_VERSION,
        _SLINKY_CHART_VERSION,
        WorkloadClusterDeployments,
    )
    from workload_cluster_infrastructure_aks import (
        AKSNodePoolSpec,
        AKSWorkloadClusterInfrastructure,
    )


_CLUSTER_CLASS = "aks"


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
    keda_namespace: pulumi.Output[str | None]
    keda_scaled_object_names: pulumi.Output[list[str]]
    keda_status: pulumi.Output[Any]
    prometheus_namespace: pulumi.Output[str]
    prometheus_status: pulumi.Output[Any]
    workload_cluster_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        workload_spec: Any,
        subscription_id: str,
        identity_name: pulumi.Input[str],
        identity_namespace: pulumi.Input[str],
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
        self.keda_namespace = deployments.keda_namespace
        self.keda_scaled_object_names = deployments.keda_scaled_object_names
        self.keda_status = deployments.keda_status
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
                "keda_chart_version": _KEDA_CHART_VERSION,
                "keda_namespace": self.keda_namespace,
                "keda_scaled_object_names": self.keda_scaled_object_names,
                "keda_status": self.keda_status,
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
