"""Workload-cluster inventory and fan-out."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

import pulumi
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from lib.config import PulumiConfigModel
from stacks.workload_cluster.workload_cluster_class_aks import (
    AKSWorkloadClusterClass,
    AKSWorkloadClusterConfig,
)
from stacks.workload_cluster.workload_cluster_class_local import (
    LocalWorkloadClusterClass,
    LocalWorkloadClusterConfig,
)


_PROJECT_NAME = "ca4s-workload-cluster"
SPEC_CONFIG_KEY = "spec"
# RFC 1123 DNS label: lowercase alphanumerics and hyphens, no leading/trailing
# hyphen, at most 63 characters.
DnsLabel = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63),
]


WorkloadClusterConfig = Annotated[
    AKSWorkloadClusterConfig | LocalWorkloadClusterConfig,
    Field(discriminator="class_name"),
]


class TenantsConfig(PulumiConfigModel):
    workload_clusters: dict[DnsLabel, WorkloadClusterConfig] = {
        "local": LocalWorkloadClusterConfig(),
    }


class WorkloadClusterContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity_name: str
    identity_namespace: str


class Tenants(pulumi.ComponentResource):
    """Instantiate workload-cluster instances from ``spec.workloadClusters``."""

    workload_clusters: pulumi.Output[list[dict[str, Any]]]

    def __init__(
        self,
        name: str,
        *,
        spec: TenantsConfig | Mapping[str, object] | None = None,
        context: pulumi.Input[WorkloadClusterContext] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:workload:Tenants", name, props={}, opts=opts)

        spec_input = (
            spec
            if spec is not None
            else pulumi.Config(_PROJECT_NAME).get_object(SPEC_CONFIG_KEY)
        )
        workload_clusters_spec = (
            spec_input
            if isinstance(spec_input, TenantsConfig)
            else TenantsConfig.model_validate(spec_input or {})
        )

        child_clusters = [
            self._instantiate_workload_cluster(
                instance_name,
                workload_cluster,
                context=context,
            )
            for instance_name, workload_cluster in (
                sorted(workload_clusters_spec.workload_clusters.items())
            )
        ]

        self.workload_clusters = pulumi.Output.all(
            *[cluster.outputs for cluster in child_clusters]
        ).apply(lambda outputs: [output.model_dump() for output in outputs])

        self.register_outputs({"workload_clusters": self.workload_clusters})

    def _instantiate_workload_cluster(
        self,
        instance_name: DnsLabel,
        workload_cluster: WorkloadClusterConfig,
        *,
        context: pulumi.Input[WorkloadClusterContext] | None,
    ) -> Any:
        if isinstance(workload_cluster, AKSWorkloadClusterConfig):
            return AKSWorkloadClusterClass(
                f"{instance_name}-workload-cluster",
                instance=instance_name,
                config=workload_cluster,
                identity_name=(
                    pulumi.Output.from_input(context).apply(
                        lambda value: value.identity_name
                    )
                    if context is not None
                    else None
                ),
                identity_namespace=(
                    pulumi.Output.from_input(context).apply(
                        lambda value: value.identity_namespace
                    )
                    if context is not None
                    else None
                ),
                opts=pulumi.ResourceOptions(parent=self),
            )

        return LocalWorkloadClusterClass(
            f"{instance_name}-workload-cluster",
            instance=instance_name,
            config=workload_cluster,
            opts=pulumi.ResourceOptions(parent=self),
        )