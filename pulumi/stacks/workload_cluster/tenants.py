"""Workload-cluster inventory and fan-out."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

import pulumi
from pydantic import Field, StringConstraints

from lib.config import PulumiConfigModel
from lib.outputs import to_output_value
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


@dataclass(frozen=True)
class WorkloadClusterContext:
    subscription_id: str | None = None
    identity_name: pulumi.Input[str] | None = None
    identity_namespace: pulumi.Input[str] | None = None


def _workload_cluster_output(cluster: Any) -> dict[str, Any]:
    return {
        name: to_output_value(value)
        for name, value in vars(cluster).items()
        if not name.startswith("_")
    }


class Tenants(pulumi.ComponentResource):
    """Instantiate workload-cluster instances from ``spec.workloadClusters``."""

    workload_clusters: list[dict[str, Any]]

    def __init__(
        self,
        name: str,
        *,
        spec: TenantsConfig | Mapping[str, object] | None = None,
        context: WorkloadClusterContext | None = None,
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
        workload_context = context or WorkloadClusterContext()

        child_clusters = [
            self._instantiate_workload_cluster(
                instance_name,
                workload_cluster,
                workload_context,
            )
            for instance_name, workload_cluster in (
                sorted(workload_clusters_spec.workload_clusters.items())
            )
        ]

        self.workload_clusters = [
            _workload_cluster_output(cluster) for cluster in child_clusters
        ]

        self.register_outputs({"workload_clusters": self.workload_clusters})

    def _instantiate_workload_cluster(
        self,
        instance_name: DnsLabel,
        workload_cluster: WorkloadClusterConfig,
        context: WorkloadClusterContext,
    ) -> Any:
        if isinstance(workload_cluster, AKSWorkloadClusterConfig):
            return AKSWorkloadClusterClass(
                f"{instance_name}-workload-cluster",
                instance=instance_name,
                config=workload_cluster,
                context=context,
                opts=pulumi.ResourceOptions(parent=self),
            )

        return LocalWorkloadClusterClass(
            f"{instance_name}-workload-cluster",
            instance=instance_name,
            config=workload_cluster,
            context=context,
            opts=pulumi.ResourceOptions(parent=self),
        )