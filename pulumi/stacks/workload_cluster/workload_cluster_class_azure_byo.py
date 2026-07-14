"""Azure BYO workload-cluster class composition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

import pulumi
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_serializer,
)

from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from localenv import discover_azure_host_network, discover_azure_resource_placement
from stacks.workload_cluster.workload_cluster_infrastructure_azure_byo import (
    AzureBYONodePoolSpec,
    AzureBYOSubnet,
    AzureBYOWorkloadClusterInfrastructure,
)


_CLUSTER_CLASS = "azure-byo"
_DEFAULT_KUBERNETES_VERSION = "v1.36.1"
_DEFAULT_CONTROL_PLANE_VM_SIZE = "Standard_D2as_v5"
_DEFAULT_WORKER_VM_SIZE = "Standard_D2as_v5"
_CONTROLLER_NODE_TYPE = "controller"
_COMPUTE_NODE_TYPE = "compute"


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
    kubernetes_version: NonEmptyStr = _DEFAULT_KUBERNETES_VERSION
    control_plane_vm_size: NonEmptyStr = _DEFAULT_CONTROL_PLANE_VM_SIZE
    worker_vm_size: NonEmptyStr = _DEFAULT_WORKER_VM_SIZE
    worker_replicas: StrictPositiveInt = 1
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
    cluster_name: str
    control_plane_name: str
    worker_machine_deployment_name: str
    worker_machine_deployment_names: list[str]
    control_plane_ready: bool
    azure_cloud_provider_chart_version: str
    azure_cloud_provider_status: Any
    calico_chart_version: str
    calico_status: Any
    workload_cluster_ready: bool
    todo: str


def _default_node_pools(
    parameters: AzureBYOWorkloadSpec,
) -> tuple[AzureBYONodePoolSpec, ...]:
    return (
        AzureBYONodePoolSpec(
            name="control-plane",
            node_type=_CONTROLLER_NODE_TYPE,
            vm_size=parameters.control_plane_vm_size,
            replicas=1,
            controller=True,
        ),
        AzureBYONodePoolSpec(
            name="compute",
            node_type=_COMPUTE_NODE_TYPE,
            vm_size=parameters.worker_vm_size,
            replicas=parameters.worker_replicas,
            attach_to_flex=True,
        ),
    )


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
        identity_name: pulumi.Input[str] | None,
        identity_namespace: pulumi.Input[str] | None,
        azure_client_id: pulumi.Input[str] | None,
        azure_tenant_id: pulumi.Input[str] | None,
        azure_identity_resource_id: pulumi.Input[str] | None,
        node_pools: tuple[AzureBYONodePoolSpec, ...] | None = None,
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
        if identity_name is None:
            raise ValueError("azure-byo workload class requires identity_name")
        if identity_namespace is None:
            raise ValueError("azure-byo workload class requires identity_namespace")
        if azure_identity_resource_id is None:
            raise ValueError(
                "azure-byo workload class requires an auto-discovered UAMI resource ID"
            )

        parameters = config.parameters
        node_pools = node_pools or _default_node_pools(parameters)
        byo_subnet = _resolve_byo_subnet(parameters)
        infrastructure = AzureBYOWorkloadClusterInfrastructure(
            "infrastructure",
            instance=instance,
            subscription_id=str(parameters.subscription_id),
            tenant_id=azure_tenant_id,
            client_id=azure_client_id,
            node_identity_resource_id=azure_identity_resource_id,
            identity_name=identity_name,
            identity_namespace=identity_namespace,
            location=parameters.location,
            additional_tags=parameters.additional_tags,
            byo_subnet=byo_subnet,
            kubernetes_version=parameters.kubernetes_version,
            node_pools=node_pools,
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
            "cluster_name": infrastructure.cluster_name,
            "control_plane_name": infrastructure.control_plane_name,
            "worker_machine_deployment_name": (
                infrastructure.worker_machine_deployment_name
            ),
            "worker_machine_deployment_names": (
                pulumi.Output.all(*infrastructure.worker_machine_deployment_names)
            ),
            "control_plane_ready": infrastructure.control_plane_ready,
            "azure_cloud_provider_chart_version": (
                infrastructure.azure_cloud_provider_chart_version
            ),
            "azure_cloud_provider_status": infrastructure.azure_cloud_provider_status,
            "calico_chart_version": infrastructure.calico_chart_version,
            "calico_status": infrastructure.calico_status,
            "workload_cluster_ready": infrastructure.workload_cluster_ready,
            "todo": pulumi.Output.from_input(
                "Validate VMSS Flex worker replacement and uninterrupted "
                "single-pass outer teardown."
            ),
        }
        self.outputs = pulumi.Output.all(**outputs).apply(
            AzureBYOWorkloadClusterOutputs.model_validate
        )
        self.register_outputs(outputs)


WorkloadClusterClass = AzureBYOWorkloadClusterClass
