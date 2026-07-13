"""Azure BYO workload infrastructure owned by the PKO init stack."""

from __future__ import annotations

import re
from collections.abc import Mapping

import pulumi
import pulumi_azure_native as azure_native
from pydantic import BaseModel, ConfigDict

from localenv import AzureHostNetwork


_FLEX_ORCHESTRATION_MODE = "Flexible"
_DEFAULT_FLEX_ZONE = "1"
_DEFAULT_PLATFORM_FAULT_DOMAIN_COUNT = 1
_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


class AzureBYOSubnet(BaseModel):
    """Resolved existing subnet consumed by the Azure BYO CAPI graph."""

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    location: str
    vnet_id: str
    vnet_name: str
    vnet_resource_group: str
    subnet_id: str
    subnet_name: str
    address_prefix: str | None = None
    network_security_group_id: str | None = None
    nat_gateway_id: str | None = None
    route_table_id: str | None = None

    @classmethod
    def from_host_network(cls, network: AzureHostNetwork) -> AzureBYOSubnet:
        return cls(
            subscription_id=network.subscription_id,
            location=network.location,
            vnet_id=network.vnet_id,
            vnet_name=network.vnet_name,
            vnet_resource_group=network.vnet_resource_group,
            subnet_id=network.subnet_id,
            subnet_name=network.subnet_name,
            address_prefix=network.subnet_address_prefix,
            network_security_group_id=network.network_security_group_id,
            nat_gateway_id=network.nat_gateway_id,
            route_table_id=network.route_table_id,
        )


def _resource_name(instance: str, suffix: str) -> str:
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", instance.lower()).strip("-")
    if not normalized:
        raise ValueError("instance must contain at least one alphanumeric character")
    normalized = normalized[: _DNS_LABEL_MAX_LENGTH - len(suffix) - 1].rstrip("-")
    return f"{normalized}-{suffix}"


def _resource_group_name(instance: str) -> str:
    return _resource_name(instance, "rg")


def _vmss_flex_name(instance: str) -> str:
    return _resource_name(instance, "flex")


def _resource_group_args(
    *,
    instance: str,
    location: str,
    additional_tags: Mapping[str, str],
) -> azure_native.resources.ResourceGroupArgs:
    return azure_native.resources.ResourceGroupArgs(
        resource_group_name=_resource_group_name(instance),
        location=location,
        tags=dict(additional_tags),
    )


def _vmss_flex_args(
    *,
    instance: str,
    location: str,
    resource_group: pulumi.Input[str],
    additional_tags: Mapping[str, str],
) -> azure_native.compute.VirtualMachineScaleSetArgs:
    """Azure Native inputs for an empty Flex placement container.

    The VMSS intentionally has no virtual-machine profile. CAPZ creates each
    AzureMachine independently and attaches it through the fork's
    ``virtualMachineScaleSetID`` field.
    """
    return azure_native.compute.VirtualMachineScaleSetArgs(
        resource_group_name=resource_group,
        vm_scale_set_name=_vmss_flex_name(instance),
        location=location,
        orchestration_mode=_FLEX_ORCHESTRATION_MODE,
        platform_fault_domain_count=_DEFAULT_PLATFORM_FAULT_DOMAIN_COUNT,
        zones=[_DEFAULT_FLEX_ZONE],
        sku=azure_native.compute.SkuArgs(capacity=0),
        tags=dict(additional_tags),
    )


class AzureBYOWorkloadClusterInfrastructure(pulumi.ComponentResource):
    """Create Azure resources that CAPZ consumes as BYO worker infrastructure."""

    resource_group_id: pulumi.Output[str]
    resource_group_name: pulumi.Output[str]
    vmss_flex_id: pulumi.Output[str]
    vmss_flex_name: pulumi.Output[str]
    byo_subnet: AzureBYOSubnet | None

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        subscription_id: str,
        tenant_id: pulumi.Input[str],
        client_id: pulumi.Input[str],
        location: str,
        additional_tags: Mapping[str, str],
        byo_subnet: AzureBYOSubnet | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:AzureBYOWorkloadClusterInfrastructure",
            name,
            props={},
            opts=opts,
        )

        azure_provider = azure_native.Provider(
            "azure-native",
            subscription_id=subscription_id,
            tenant_id=tenant_id,
            client_id=client_id,
            use_msi=True,
            opts=pulumi.ResourceOptions(parent=self),
        )
        resource_group = azure_native.resources.ResourceGroup(
            "resource-group",
            _resource_group_args(
                instance=instance,
                location=location,
                additional_tags=additional_tags,
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=azure_provider,
            ),
        )
        flex = azure_native.compute.VirtualMachineScaleSet(
            "vmss-flex",
            _vmss_flex_args(
                instance=instance,
                location=location,
                resource_group=resource_group.name,
                additional_tags=additional_tags,
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=azure_provider,
                delete_before_replace=True,
                replace_on_changes=[
                    "orchestrationMode",
                    "platformFaultDomainCount",
                    "zones",
                ],
                custom_timeouts=pulumi.CustomTimeouts(
                    create="30m",
                    update="30m",
                    delete="30m",
                ),
            ),
        )

        self.resource_group_id = resource_group.id
        self.resource_group_name = resource_group.name
        self.vmss_flex_id = flex.id
        self.vmss_flex_name = flex.name
        self.byo_subnet = byo_subnet
        self.register_outputs(
            {
                "resource_group_id": self.resource_group_id,
                "resource_group_name": self.resource_group_name,
                "vmss_flex_id": self.vmss_flex_id,
                "vmss_flex_name": self.vmss_flex_name,
                "byo_subnet": (
                    self.byo_subnet.model_dump() if self.byo_subnet is not None else None
                ),
            }
        )
