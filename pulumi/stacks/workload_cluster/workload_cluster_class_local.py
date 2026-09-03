# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Local workload-cluster class composition."""

from __future__ import annotations

from typing import Any, Literal

import pulumi
from pydantic import BaseModel, ConfigDict, field_serializer

from lib.config import PulumiConfigModel
from stacks.workload_cluster.registry_setting import RegistryConfig

from stacks.workload_cluster.workload_cluster_deployments import (
    KEDAOutputs,
    KEDANodeSetScalerSpec,
    SlinkyDeploymentConfig,
    SlurmNodeSetSpec,
    _PROMETHEUS_CHART_VERSION,
    WorkloadClusterDeployments,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    ClusterAPIAutoscalerOutputs,
    COMPUTE_NODE_TYPE,
    CONTROLLER_NODE_TYPE,
)
from stacks.workload_cluster.workload_cluster_infrastructure_local import (
    LocalMachineDeploymentSpec,
    LocalWorkloadClusterInfrastructure,
)


_CLUSTER_CLASS = "local"

_LOCAL_MACHINE_DEPLOYMENTS = (
    LocalMachineDeploymentSpec(
        name="head",
        node_type=CONTROLLER_NODE_TYPE,
        replicas=1,
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


class LocalWorkloadClusterConfig(PulumiConfigModel):
    class_name: Literal["local"] = _CLUSTER_CLASS
    registry: RegistryConfig | None = None
    slinky: SlinkyDeploymentConfig = SlinkyDeploymentConfig()

    @field_serializer("class_name")
    def serialize_class_name(self, class_name: str) -> str:
        return class_name


class LocalWorkloadClusterOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    cluster_class: str
    cluster_instance: str
    cluster_name: str
    docker_cluster_name: str
    control_plane_name: str
    worker_machine_deployments: list[str]
    cluster_autoscaler: ClusterAPIAutoscalerOutputs | None
    keda: KEDAOutputs | None
    prometheus_chart_version: str
    prometheus_namespace: str
    prometheus_status: Any
    calico_operator_chart_version: str
    calico_operator_status: Any
    workload_cluster_ready: bool
    slurm_operator_chart_version: str
    slurm_operator_status: Any
    slurm_chart_version: str
    slurm_status: Any
    todo: str


class LocalWorkloadClusterClass(pulumi.ComponentResource):
    """Reusable local workload-cluster class.

    A class captures the resource graph shape. The ``instance`` passed to
    the constructor supplies the concrete identity used for Kubernetes object
    names and Pulumi outputs.
    """

    outputs: pulumi.Output[LocalWorkloadClusterOutputs]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        config: LocalWorkloadClusterConfig,
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
            registry=config.registry,
            opts=child_options(),
        )
        deployments = WorkloadClusterDeployments(
            "deployments",
            instance=instance,
            slurm_node_sets=slurm_node_sets,
            keda_scaled_node_sets=keda_scaled_node_sets,
            slinky=config.slinky,
            workload_provider=infrastructure.workload_provider,
            pin_coredns_to_controller=True,
            opts=child_options(depends_on=[infrastructure]),
        )

        outputs = {
            "cluster_class": pulumi.Output.from_input(_CLUSTER_CLASS),
            "cluster_instance": pulumi.Output.from_input(instance),
            "cluster_name": infrastructure.cluster_name,
            "docker_cluster_name": infrastructure.docker_cluster_name,
            "control_plane_name": infrastructure.control_plane_name,
            "worker_machine_deployments": pulumi.Output.all(
                *infrastructure.worker_machine_deployments
            ),
            "cluster_autoscaler": (
                infrastructure.cluster_autoscaler.apply(lambda value: value.model_dump())
                if infrastructure.cluster_autoscaler is not None
                else None
            ),
            "keda": (
                deployments.keda.apply(lambda value: value.model_dump())
                if deployments.keda is not None
                else None
            ),
            "prometheus_chart_version": _PROMETHEUS_CHART_VERSION,
            "prometheus_namespace": deployments.prometheus_namespace,
            "prometheus_status": deployments.prometheus_status,
            "calico_operator_chart_version": infrastructure.calico_operator_chart_version,
            "calico_operator_status": infrastructure.calico_operator_status,
            "workload_cluster_ready": deployments.workload_cluster_ready,
            "slurm_operator_chart_version": config.slinky.operator_chart_version,
            "slurm_operator_status": deployments.slurm_operator_status,
            "slurm_chart_version": config.slinky.slurm_chart_version,
            "slurm_status": deployments.slurm_status,
            "todo": pulumi.Output.from_input(
                "Wire workload-driven autoscaling and tenant-facing Slurm operations."
            ),
        }

        self.outputs = pulumi.Output.all(**outputs).apply(
            LocalWorkloadClusterOutputs.model_validate
        )
        self.register_outputs(outputs)


WorkloadClusterClass = LocalWorkloadClusterClass
