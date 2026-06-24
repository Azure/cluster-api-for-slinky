"""Local workload-cluster class composition."""

from __future__ import annotations

from typing import Any, Mapping

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
    from .workload_cluster_infrastructure_local import (
        LocalMachineDeploymentSpec,
        LocalWorkloadClusterInfrastructure,
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
    from workload_cluster_infrastructure_local import (
        LocalMachineDeploymentSpec,
        LocalWorkloadClusterInfrastructure,
    )


_CLUSTER_CLASS = "local"

_LOCAL_MACHINE_DEPLOYMENTS = (
    LocalMachineDeploymentSpec(
        name="head",
        node_type=CONTROLLER_NODE_TYPE,
        replicas=1,
        controller=True,
    ),
    LocalMachineDeploymentSpec(
        name="compute",
        node_type=COMPUTE_NODE_TYPE,
        replicas=1,
        autoscaler_bounds=(1, 10),
    ),
)
_LOCAL_SLURM_NODE_SETS = (
    SlurmNodeSetSpec(name="compute", node_type=COMPUTE_NODE_TYPE, replicas=1),
)
_LOCAL_KEDA_SCALED_NODE_SETS = (
    KEDANodeSetScalerSpec(node_set_name="compute", min_replicas=1, max_replicas=10),
)


class LocalWorkloadClusterClass(pulumi.ComponentResource):
    """Reusable local workload-cluster class.

    A class captures the resource graph shape. The ``instance`` passed to
    the constructor supplies the concrete identity used for Kubernetes object
    names and Pulumi outputs.
    """

    cluster_class: pulumi.Output[str]
    cluster_instance: pulumi.Output[str]
    cluster_name: pulumi.Output[str]
    docker_cluster_name: pulumi.Output[str]
    control_plane_name: pulumi.Output[str]
    worker_machine_deployments: list[pulumi.Output[str]]
    cluster_autoscaler_namespace: pulumi.Output[str | None]
    cluster_autoscaler_status: pulumi.Output[Any]
    keda_namespace: pulumi.Output[str | None]
    keda_scaled_object_names: pulumi.Output[list[str]]
    keda_status: pulumi.Output[Any]
    prometheus_namespace: pulumi.Output[str]
    prometheus_status: pulumi.Output[Any]
    calico_operator_chart_version: pulumi.Output[str | None]
    calico_operator_status: pulumi.Output[Any]
    workload_cluster_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        parameters: Mapping[str, object] | None = None,
        context: Any | None = None,
        machine_deployments: tuple[LocalMachineDeploymentSpec, ...] = _LOCAL_MACHINE_DEPLOYMENTS,
        slurm_node_sets: tuple[SlurmNodeSetSpec, ...] = _LOCAL_SLURM_NODE_SETS,
        keda_scaled_node_sets: tuple[KEDANodeSetScalerSpec, ...] = _LOCAL_KEDA_SCALED_NODE_SETS,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Build one local workload-cluster instance from this class."""
        super().__init__(
            "ca4s:workload:LocalWorkloadClusterClass",
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

        infrastructure = LocalWorkloadClusterInfrastructure(
            "infrastructure",
            instance=instance,
            worker_machine_deployments=machine_deployments,
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
        self.docker_cluster_name = infrastructure.docker_cluster_name
        self.control_plane_name = infrastructure.control_plane_name
        self.worker_machine_deployments = infrastructure.worker_machine_deployments
        self.cluster_autoscaler_namespace = infrastructure.cluster_autoscaler_namespace
        self.cluster_autoscaler_status = infrastructure.cluster_autoscaler_status
        self.keda_namespace = deployments.keda_namespace
        self.keda_scaled_object_names = deployments.keda_scaled_object_names
        self.keda_status = deployments.keda_status
        self.prometheus_namespace = deployments.prometheus_namespace
        self.prometheus_status = deployments.prometheus_status
        self.calico_operator_chart_version = infrastructure.calico_operator_chart_version
        self.calico_operator_status = infrastructure.calico_operator_status
        self.workload_cluster_ready = deployments.workload_cluster_ready
        self.todo = pulumi.Output.from_input(
            "Wire workload-driven autoscaling and tenant-facing Slurm operations."
        )

        self.register_outputs(
            {
                "cluster_class": self.cluster_class,
                "cluster_instance": self.cluster_instance,
                "cluster_name": self.cluster_name,
                "docker_cluster_name": self.docker_cluster_name,
                "control_plane_name": self.control_plane_name,
                "worker_machine_deployments": self.worker_machine_deployments,
                "cluster_autoscaler_namespace": self.cluster_autoscaler_namespace,
                "cluster_autoscaler_status": self.cluster_autoscaler_status,
                "keda_chart_version": _KEDA_CHART_VERSION,
                "keda_namespace": self.keda_namespace,
                "keda_scaled_object_names": self.keda_scaled_object_names,
                "keda_status": self.keda_status,
                "prometheus_chart_version": _PROMETHEUS_CHART_VERSION,
                "prometheus_namespace": self.prometheus_namespace,
                "prometheus_status": self.prometheus_status,
                "calico_operator_chart_version": self.calico_operator_chart_version,
                "calico_operator_status": self.calico_operator_status,
                "workload_cluster_ready": self.workload_cluster_ready,
                "slurm_operator_chart_version": _SLINKY_CHART_VERSION,
                "slurm_operator_status": deployments.slurm_operator_status,
                "slurm_chart_version": _SLINKY_CHART_VERSION,
                "slurm_status": deployments.slurm_status,
                "todo": self.todo,
            }
        )


WorkloadClusterClass = LocalWorkloadClusterClass
