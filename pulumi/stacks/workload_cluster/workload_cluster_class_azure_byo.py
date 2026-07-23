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
from stacks.workload_cluster.workload_cluster_deployments import (
    KEDAOutputs,
    KEDANodeSetScalerSpec,
    SlurmNodeSetSpec,
    _PROMETHEUS_CHART_VERSION,
    _SLINKY_CHART_VERSION,
    WorkloadClusterDeployments,
)
from stacks.workload_cluster.workload_cluster_infrastructure import (
    ClusterAPIAutoscalerOutputs,
)
from stacks.workload_cluster.workload_cluster_infrastructure_azure_byo import (
    AzureBYOMarketplaceImage,
    AzureBYONodePoolSpec,
    AzureBYOSubnet,
    AzureBYOWorkloadClusterInfrastructure,
)


_CLUSTER_CLASS = "azure-byo"
_DEFAULT_KUBERNETES_VERSION = "v1.36.1"
_DEFAULT_CONTROL_PLANE_VM_SIZE = "Standard_D2as_v5"
_DEFAULT_WORKER_VM_SIZE = "Standard_D2as_v5"
_DEFAULT_SSH_USERNAME = "capi"
_CONTROLLER_NODE_TYPE = "controller"
_COMPUTE_NODE_TYPE = "compute"

_AZURE_BYO_SLURM_NODE_SETS = (
    SlurmNodeSetSpec(name="compute", node_type=_COMPUTE_NODE_TYPE, replicas=1),
)
_AZURE_BYO_KEDA_SCALED_NODE_SETS = (
    KEDANodeSetScalerSpec(node_set_name="compute", min_replicas=1, max_replicas=10),
)


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
    # Optional V100-compatible Marketplace image for GPU workers (NDV2/V100).
    # Unset => CAPZ default Ubuntu (CPU-only). TODO(caps): add control_plane_image
    # when the head node also needs the HPC image; CP stays CPU for NCCL host-launch.
    worker_image: AzureBYOMarketplaceImage | None = None
    # Shared Image Gallery image-version resource IDs to boot the nodes from (e.g. the HPC
    # image under validation, passed through from the pipeline). Rendered as CAPZ image.id
    # and take precedence over worker_image; the image ships no Kubernetes, so CAPZ installs
    # it at bootstrap. control_plane_image_id defaults to worker_image_id when unset so both
    # roles boot the same image; set it explicitly to keep the control plane on another image.
    worker_image_id: NonEmptyStr | None = None
    control_plane_image_id: NonEmptyStr | None = None
    ssh_username: NonEmptyStr = _DEFAULT_SSH_USERNAME
    ssh_authorized_keys: tuple[NonEmptyStr, ...] = ()
    vnet: AzureBYOVNetConfig | None = None
    use_auto_discovered_vnet: StrictBool | None = None
    # Expose the control-plane (head node) API server on a public IP. Default
    # False keeps the apiServerLB Internal (reached via the mgmt VM, the caps-self
    # model). When True, CAPZ fronts the API server with a public LB + public IP,
    # and the required Azure Security Pack tag (AzSecPackAutoConfigReady) is
    # applied to the cluster's Azure resources so the public IP satisfies corp
    # security policy.
    control_plane_public: StrictBool = False
    # Reuse the resource group that Azure IMDS host discovery selects (the RG of
    # the VM running Docker + the Kind management cluster) instead of registering
    # a new Pulumi-owned "<instance>-rg". CAPZ treats an untagged pre-existing RG
    # as unmanaged: deleting the cluster removes its resources individually but
    # preserves the shared host RG. The discovered RG must be in the workload
    # subscription; its own location does not constrain resources placed in it.
    use_discovered_resource_group: StrictBool = False

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
    local_path_storage_class_name: str
    cluster_autoscaler: ClusterAPIAutoscalerOutputs | None
    keda: KEDAOutputs | None
    prometheus_chart_version: str
    prometheus_namespace: str
    prometheus_status: Any
    slurm_operator_chart_version: str
    slurm_operator_status: Any
    slurm_chart_version: str
    slurm_status: Any
    workload_cluster_ready: bool
    todo: str


def _default_node_pools(
    parameters: AzureBYOWorkloadSpec,
) -> tuple[AzureBYONodePoolSpec, ...]:
    return (
        AzureBYONodePoolSpec.model_validate(
            {
                "name": "control-plane",
                "node_type": _CONTROLLER_NODE_TYPE,
                "vm_size": parameters.control_plane_vm_size,
                "replicas": 1,
                "controller": True,
                # The control plane boots the same gallery image unless overridden.
                "image_id": parameters.control_plane_image_id or parameters.worker_image_id,
            }
        ),
        AzureBYONodePoolSpec.model_validate(
            {
                "name": "compute",
                "node_type": _COMPUTE_NODE_TYPE,
                "vm_size": parameters.worker_vm_size,
                "replicas": parameters.worker_replicas,
                "attach_to_flex": True,
                "autoscaler_bounds": (1, 10),
                # Compute (worker) nodes get the optional V100 image; the control
                # plane above intentionally stays on the CAPZ default Ubuntu image.
                "image": parameters.worker_image,
                "image_id": parameters.worker_image_id,
            }
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


def _resolve_resource_group(parameters: AzureBYOWorkloadSpec) -> str | None:
    if not parameters.use_discovered_resource_group:
        return None

    placement = discover_azure_resource_placement(raise_on_missing=True)
    if placement.subscription_id.casefold() != str(parameters.subscription_id).casefold():
        raise ValueError(
            "auto-discovered resource group subscription does not match "
            "azure-byo subscriptionId"
        )
    return placement.resource_group


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
        slurm_node_sets: tuple[
            SlurmNodeSetSpec, ...
        ] = _AZURE_BYO_SLURM_NODE_SETS,
        keda_scaled_node_sets: tuple[
            KEDANodeSetScalerSpec, ...
        ] = _AZURE_BYO_KEDA_SCALED_NODE_SETS,
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
        resource_group_name = _resolve_resource_group(parameters)
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
            api_server_public=parameters.control_plane_public,
            resource_group_name=resource_group_name,
            byo_subnet=byo_subnet,
            kubernetes_version=parameters.kubernetes_version,
            ssh_username=parameters.ssh_username,
            ssh_authorized_keys=parameters.ssh_authorized_keys,
            node_pools=node_pools,
            opts=pulumi.ResourceOptions(parent=self),
        )
        deployments = WorkloadClusterDeployments(
            "deployments",
            instance=instance,
            slurm_node_sets=slurm_node_sets,
            keda_scaled_node_sets=keda_scaled_node_sets,
            workload_provider=infrastructure.workload_provider,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[infrastructure],
            ),
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
            "local_path_storage_class_name": (
                infrastructure.local_path_storage_class_name
            ),
            "cluster_autoscaler": (
                infrastructure.cluster_autoscaler.apply(
                    lambda value: value.model_dump()
                )
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
            "slurm_operator_chart_version": _SLINKY_CHART_VERSION,
            "slurm_operator_status": deployments.slurm_operator_status,
            "slurm_chart_version": _SLINKY_CHART_VERSION,
            "slurm_status": deployments.slurm_status,
            "workload_cluster_ready": pulumi.Output.all(
                infrastructure.workload_cluster_ready,
                deployments.workload_cluster_ready,
            ).apply(lambda _: True),
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
