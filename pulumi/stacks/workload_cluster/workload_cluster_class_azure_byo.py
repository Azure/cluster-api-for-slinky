"""Azure BYO workload-cluster class composition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

import pulumi
from pydantic import BaseModel, ConfigDict, Field, field_serializer

from lib.config import NonEmptyStr, PulumiConfigModel
from localenv import discover_azure_resource_placement
from stacks.workload_cluster.workload_cluster_infrastructure_azure_byo import (
    AzureBYOWorkloadClusterInfrastructure,
)


_CLUSTER_CLASS = "azure-byo"


class AzureBYOWorkloadSpec(PulumiConfigModel):
    """Azure subscription, location, and tags for one BYO workload."""

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
    additional_tags: Mapping[NonEmptyStr, str] = Field(default_factory=dict)

    @field_serializer("subscription_id")
    def serialize_subscription_id(self, value: UUID) -> str:
        return str(value)

    @field_serializer("additional_tags")
    def serialize_additional_tags(
        self,
        additional_tags: Mapping[NonEmptyStr, str],
    ) -> dict[str, str]:
        return dict(additional_tags)


class AzureBYOWorkloadClusterConfig(PulumiConfigModel):
    class_name: Literal["azure-byo"] = _CLUSTER_CLASS
    parameters: AzureBYOWorkloadSpec

    @field_serializer("class_name")
    def serialize_class_name(self, class_name: str) -> str:
        return class_name


class AzureBYOWorkloadClusterOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    cluster_class: str
    cluster_instance: str
    resource_group_id: str
    resource_group_name: str
    vmss_flex_id: str
    vmss_flex_name: str
    todo: str


class AzureBYOWorkloadClusterClass(pulumi.ComponentResource):
    """Azure BYO class whose first increment owns the Flex placement VMSS."""

    outputs: pulumi.Output[AzureBYOWorkloadClusterOutputs]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        config: AzureBYOWorkloadClusterConfig,
        azure_client_id: pulumi.Input[str] | None,
        azure_tenant_id: pulumi.Input[str] | None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:AzureBYOWorkloadClusterClass",
            name,
            props={},
            opts=opts,
        )
        if azure_client_id is None:
            raise ValueError("azure-byo workload class requires azure_client_id")
        if azure_tenant_id is None:
            raise ValueError("azure-byo workload class requires azure_tenant_id")

        parameters = config.parameters
        infrastructure = AzureBYOWorkloadClusterInfrastructure(
            "infrastructure",
            instance=instance,
            subscription_id=str(parameters.subscription_id),
            tenant_id=azure_tenant_id,
            client_id=azure_client_id,
            location=parameters.location,
            additional_tags=parameters.additional_tags,
            opts=pulumi.ResourceOptions(parent=self),
        )

        outputs = {
            "cluster_class": pulumi.Output.from_input(_CLUSTER_CLASS),
            "cluster_instance": pulumi.Output.from_input(instance),
            "resource_group_id": infrastructure.resource_group_id,
            "resource_group_name": infrastructure.resource_group_name,
            "vmss_flex_id": infrastructure.vmss_flex_id,
            "vmss_flex_name": infrastructure.vmss_flex_name,
            "todo": pulumi.Output.from_input(
                "Attach the compute MachineDeployment through virtualMachineScaleSetID."
            ),
        }
        self.outputs = pulumi.Output.all(**outputs).apply(
            AzureBYOWorkloadClusterOutputs.model_validate
        )
        self.register_outputs(outputs)


WorkloadClusterClass = AzureBYOWorkloadClusterClass
