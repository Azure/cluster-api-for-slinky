"""Azure BYO workload-cluster class composition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

import pulumi
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_serializer,
)

from lib.config import NonEmptyStr, PulumiConfigModel
from localenv import discover_azure_host_network, discover_azure_resource_placement
from stacks.workload_cluster.workload_cluster_infrastructure_azure_byo import (
    AzureBYOSubnet,
    AzureBYOWorkloadClusterInfrastructure,
)


_CLUSTER_CLASS = "azure-byo"


class AzureBYOSubnetConfig(PulumiConfigModel):
    """Existing subnet in the explicitly selected VNet."""

    name: NonEmptyStr
    address_prefix: NonEmptyStr | None = None


class AzureBYOVNetConfig(PulumiConfigModel):
    """Existing Azure VNet and its cluster subnet selected by the user."""

    name: NonEmptyStr
    resource_group: NonEmptyStr
    subnet: AzureBYOSubnetConfig


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
    vnet: AzureBYOVNetConfig | None = None
    use_auto_discovered_vnet: StrictBool | None = None

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
    byo_subnet: dict[str, object] | None
    todo: str


def _azure_resource_id(
    *,
    subscription_id: UUID,
    resource_group: str,
    provider_path: str,
) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/{provider_path}"
    )


def _explicit_byo_subnet(parameters: AzureBYOWorkloadSpec) -> AzureBYOSubnet | None:
    if parameters.vnet is None:
        return None
    vnet_id = _azure_resource_id(
        subscription_id=parameters.subscription_id,
        resource_group=parameters.vnet.resource_group,
        provider_path=f"Microsoft.Network/virtualNetworks/{parameters.vnet.name}",
    )
    return AzureBYOSubnet(
        subscription_id=str(parameters.subscription_id),
        location=parameters.location,
        vnet_id=vnet_id,
        vnet_name=parameters.vnet.name,
        vnet_resource_group=parameters.vnet.resource_group,
        subnet_id=f"{vnet_id}/subnets/{parameters.vnet.subnet.name}",
        subnet_name=parameters.vnet.subnet.name,
        address_prefix=parameters.vnet.subnet.address_prefix,
    )


def _resolve_byo_subnet(parameters: AzureBYOWorkloadSpec) -> AzureBYOSubnet | None:
    explicit = _explicit_byo_subnet(parameters)
    if parameters.use_auto_discovered_vnet is not True and explicit is not None:
        return explicit
    if parameters.use_auto_discovered_vnet is False:
        return None
    network = discover_azure_host_network(raise_on_missing=True)
    if network.subscription_id.casefold() != str(parameters.subscription_id).casefold():
        raise ValueError(
            "auto-discovered VNet subscription does not match azure-byo subscriptionId"
        )
    if network.location.casefold() != parameters.location.casefold():
        raise ValueError(
            "auto-discovered VNet location does not match azure-byo location"
        )
    return AzureBYOSubnet.from_host_network(network)


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
        byo_subnet = _resolve_byo_subnet(parameters)
        infrastructure = AzureBYOWorkloadClusterInfrastructure(
            "infrastructure",
            instance=instance,
            subscription_id=str(parameters.subscription_id),
            tenant_id=azure_tenant_id,
            client_id=azure_client_id,
            location=parameters.location,
            additional_tags=parameters.additional_tags,
            byo_subnet=byo_subnet,
            opts=pulumi.ResourceOptions(parent=self),
        )

        outputs = {
            "cluster_class": pulumi.Output.from_input(_CLUSTER_CLASS),
            "cluster_instance": pulumi.Output.from_input(instance),
            "resource_group_id": infrastructure.resource_group_id,
            "resource_group_name": infrastructure.resource_group_name,
            "vmss_flex_id": infrastructure.vmss_flex_id,
            "vmss_flex_name": infrastructure.vmss_flex_name,
            "byo_subnet": (
                infrastructure.byo_subnet.model_dump()
                if infrastructure.byo_subnet is not None
                else None
            ),
            "todo": pulumi.Output.from_input(
                "Attach the compute MachineDeployment through virtualMachineScaleSetID."
            ),
        }
        self.outputs = pulumi.Output.all(**outputs).apply(
            AzureBYOWorkloadClusterOutputs.model_validate
        )
        self.register_outputs(outputs)


WorkloadClusterClass = AzureBYOWorkloadClusterClass
