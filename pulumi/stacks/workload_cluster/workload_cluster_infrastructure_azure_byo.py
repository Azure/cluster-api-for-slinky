"""Azure BYO workload infrastructure owned by the PKO init stack."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping

import pulumi
import pulumi_azure_native as azure_native
import pulumi_kubernetes as k8s
from pydantic import BaseModel, ConfigDict, StrictBool

from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from localenv import AzureHostNetwork
from stacks.kubernetes_annotations import (
    PULUMI_SKIP_AWAIT_ANNOTATION,
    foreground_delete_annotations,
    pulumi_wait_for,
)
from stacks.workload_cluster.workload_cluster_addons import (
    AzureCloudProvider,
    CalicoVXLAN,
)


_FLEX_ORCHESTRATION_MODE = "Flexible"
_DEFAULT_FLEX_ZONE = "1"
_DEFAULT_PLATFORM_FAULT_DOMAIN_COUNT = 1
_CAPI_API_VERSION = "cluster.x-k8s.io/v1beta1"
_BOOTSTRAP_API_VERSION = "bootstrap.cluster.x-k8s.io/v1beta1"
_CONTROL_PLANE_API_VERSION = "controlplane.cluster.x-k8s.io/v1beta1"
_INFRASTRUCTURE_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta1"
_NAMESPACE = "default"
_POD_CIDR = "192.168.0.0/16"
_SERVICE_CIDR = "10.96.0.0/12"
_SERVICE_DOMAIN = "cluster.local"
_WORKLOAD_KUBECONFIG_SECRET_KEY = "value"
_WAIT_FOR_CONTROL_PLANE_AVAILABLE = "condition=ControlPlaneAvailable"
_AZURE_PROVIDER_ID_PREFIX = "azure://"
_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


class AzureBYONodePoolSpec(PulumiConfigModel):
    """One self-managed Azure node role rendered through CAPI resources."""

    name: NonEmptyStr
    node_type: NonEmptyStr
    vm_size: NonEmptyStr
    replicas: StrictPositiveInt
    controller: StrictBool = False
    attach_to_flex: StrictBool = False
    failure_domain: NonEmptyStr = _DEFAULT_FLEX_ZONE


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


def _cluster_name(instance: str) -> str:
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", instance.lower()).strip("-")
    if not normalized:
        raise ValueError("instance must contain at least one alphanumeric character")
    return normalized[:_DNS_LABEL_MAX_LENGTH].rstrip("-")


def _partition_node_pools(
    node_pools: tuple[AzureBYONodePoolSpec, ...],
) -> tuple[AzureBYONodePoolSpec, tuple[AzureBYONodePoolSpec, ...]]:
    controllers = tuple(node for node in node_pools if node.controller)
    workers = tuple(node for node in node_pools if not node.controller)
    if len(controllers) != 1:
        raise ValueError("azure-byo requires exactly one controller node spec")
    if not workers:
        raise ValueError("azure-byo requires at least one worker node spec")
    return controllers[0], workers


def _resource_group_name(instance: str) -> str:
    return _resource_name(instance, "rg")


def _vmss_flex_name(instance: str) -> str:
    return _resource_name(instance, "flex")


def _object_ref(
    api_version: str,
    kind: str,
    name: pulumi.Input[str],
) -> dict[str, object]:
    return {"apiVersion": api_version, "kind": kind, "name": name}


def _resource_id_name(resource_id: str | None) -> str | None:
    return resource_id.rstrip("/").rsplit("/", 1)[-1] if resource_id else None


def _cluster_spec(*, cluster_name: str, control_plane_name: str) -> dict[str, object]:
    return {
        "clusterNetwork": {
            "pods": {"cidrBlocks": [_POD_CIDR]},
            "services": {"cidrBlocks": [_SERVICE_CIDR]},
            "serviceDomain": _SERVICE_DOMAIN,
        },
        "controlPlaneRef": _object_ref(
            _CONTROL_PLANE_API_VERSION,
            "KubeadmControlPlane",
            control_plane_name,
        ),
        "infrastructureRef": _object_ref(
            _INFRASTRUCTURE_API_VERSION,
            "AzureCluster",
            cluster_name,
        ),
    }


def _azure_cluster_spec(
    *,
    cluster_name: str,
    identity_name: pulumi.Input[str],
    identity_namespace: pulumi.Input[str],
    location: str,
    resource_group: pulumi.Input[str],
    subscription_id: str,
    subnet: AzureBYOSubnet,
    additional_tags: Mapping[str, str],
) -> dict[str, object]:
    cluster_subnet: dict[str, object] = {
        "name": subnet.subnet_name,
        "role": "cluster",
    }
    security_group_name = _resource_id_name(subnet.network_security_group_id)
    route_table_name = _resource_id_name(subnet.route_table_id)
    nat_gateway_name = _resource_id_name(subnet.nat_gateway_id)
    if security_group_name is not None:
        cluster_subnet["securityGroup"] = {"name": security_group_name}
    if route_table_name is not None:
        cluster_subnet["routeTable"] = {"name": route_table_name}
    if nat_gateway_name is not None:
        cluster_subnet["natGateway"] = {"name": nat_gateway_name}

    return {
        "identityRef": {
            **_object_ref(
                _INFRASTRUCTURE_API_VERSION,
                "AzureClusterIdentity",
                identity_name,
            ),
            "namespace": identity_namespace,
        },
        "location": location,
        "resourceGroup": resource_group,
        "subscriptionID": subscription_id,
        "networkSpec": {
            "vnet": {
                "name": subnet.vnet_name,
                "resourceGroup": subnet.vnet_resource_group,
            },
            "subnets": [cluster_subnet],
            "apiServerLB": {
                "name": f"{cluster_name}-internal-lb",
                "type": "Internal",
                "availabilityZones": [_DEFAULT_FLEX_ZONE],
            },
            "controlPlaneOutboundLB": {"frontendIPsCount": 1},
            "nodeOutboundLB": {"frontendIPsCount": 1},
        },
        "additionalTags": dict(additional_tags),
    }


def _machine_template_spec(
    *,
    node: AzureBYONodePoolSpec,
    subnet_name: str,
    node_identity_provider_id: pulumi.Input[str],
    additional_tags: Mapping[str, str],
    virtual_machine_scale_set_id: pulumi.Input[str] | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "vmSize": node.vm_size,
        "osDisk": {
            "diskSizeGB": 128,
            "managedDisk": {"storageAccountType": "Premium_LRS"},
            "osType": "Linux",
        },
        "identity": "UserAssigned",
        "userAssignedIdentities": [{"providerID": node_identity_provider_id}],
        "networkInterfaces": [{"subnetName": subnet_name}],
        "additionalTags": dict(additional_tags),
    }
    if virtual_machine_scale_set_id is not None:
        spec["virtualMachineScaleSetID"] = virtual_machine_scale_set_id
    return {"template": {"spec": spec}}


def _cloud_config_file(*, secret_name: str, key: str) -> dict[str, object]:
    return {
        "contentFrom": {"secret": {"name": secret_name, "key": key}},
        "owner": "root:root",
        "path": "/etc/kubernetes/azure.json",
        "permissions": "0644",
    }


def _node_registration(*, node_type: str | None = None) -> dict[str, object]:
    extra_args: dict[str, str] = {"cloud-provider": "external"}
    if node_type is not None:
        extra_args["node-labels"] = f"slinky.slurm.net/node-type={node_type}"
    return {
        "name": '{{ ds.meta_data["local_hostname"] }}',
        "kubeletExtraArgs": extra_args,
    }


def _kubeadm_control_plane_spec(
    *,
    node: AzureBYONodePoolSpec,
    cluster_name: str,
    control_plane_name: str,
    kubernetes_version: str,
) -> dict[str, object]:
    return {
        "replicas": node.replicas,
        "version": kubernetes_version,
        "machineTemplate": {
            "infrastructureRef": _object_ref(
                _INFRASTRUCTURE_API_VERSION,
                "AzureMachineTemplate",
                control_plane_name,
            ),
        },
        "kubeadmConfigSpec": {
            "clusterConfiguration": {
                "controllerManager": {
                    "extraArgs": {
                        "allocate-node-cidrs": "false",
                        "cloud-provider": "external",
                        "cluster-name": cluster_name,
                    }
                }
            },
            "files": [
                _cloud_config_file(
                    secret_name=f"{control_plane_name}-azure-json",
                    key="control-plane-azure.json",
                )
            ],
            "initConfiguration": {
                "nodeRegistration": _node_registration(node_type=node.node_type)
            },
            "joinConfiguration": {
                "nodeRegistration": _node_registration(node_type=node.node_type)
            },
            "preKubeadmCommands": [
                (
                    "if [ -f /tmp/kubeadm.yaml ] || "
                    "[ -f /run/kubeadm/kubeadm.yaml ]; then "
                    f"echo '127.0.0.1 apiserver.{cluster_name}.capz.io apiserver' "
                    ">> /etc/hosts; fi"
                )
            ],
            "postKubeadmCommands": [
                (
                    "if [ -f /tmp/kubeadm-join-config.yaml ] || "
                    "[ -f /run/kubeadm/kubeadm-join-config.yaml ]; then "
                    f"echo '127.0.0.1 apiserver.{cluster_name}.capz.io apiserver' "
                    ">> /etc/hosts; fi"
                )
            ],
        },
    }


def _machine_deployment_spec(
    *,
    node: AzureBYONodePoolSpec,
    cluster_name: str,
    worker_name: str,
    kubernetes_version: str,
) -> dict[str, object]:
    labels = {
        f"{cluster_name}.worker": node.name,
        "slinky.slurm.net/node-type": node.node_type,
    }
    return {
        "clusterName": cluster_name,
        "replicas": node.replicas,
        "selector": {"matchLabels": labels},
        "template": {
            "metadata": {"labels": labels},
            "spec": {
                "clusterName": cluster_name,
                "version": kubernetes_version,
                "failureDomain": node.failure_domain,
                "bootstrap": {
                    "configRef": _object_ref(
                        _BOOTSTRAP_API_VERSION,
                        "KubeadmConfigTemplate",
                        worker_name,
                    )
                },
                "infrastructureRef": _object_ref(
                    _INFRASTRUCTURE_API_VERSION,
                    "AzureMachineTemplate",
                    worker_name,
                ),
            },
        },
    }


def _kubeadm_config_template_spec(
    *,
    node: AzureBYONodePoolSpec,
    worker_name: str,
) -> dict[str, object]:
    return {
        "template": {
            "spec": {
                "files": [
                    _cloud_config_file(
                        secret_name=f"{worker_name}-azure-json",
                        key="worker-node-azure.json",
                    )
                ],
                "joinConfiguration": {
                    "nodeRegistration": _node_registration(node_type=node.node_type)
                },
            }
        }
    }


def _decode_secret_data_value(data: Mapping[str, str], key: str) -> str:
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


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
        tags=dict(additional_tags),
    )


class AzureBYOWorkloadClusterInfrastructure(pulumi.ComponentResource):
    """Create Azure resources that CAPZ consumes as BYO worker infrastructure."""

    resource_group_id: pulumi.Output[str]
    resource_group_name: pulumi.Output[str]
    vmss_flex_id: pulumi.Output[str]
    vmss_flex_name: pulumi.Output[str]
    byo_subnet: AzureBYOSubnet | None
    cluster_name: pulumi.Output[str]
    control_plane_name: pulumi.Output[str]
    worker_machine_deployment_name: pulumi.Output[str]
    worker_machine_deployment_names: list[pulumi.Output[str]]
    control_plane_ready: pulumi.Output[bool]
    workload_kubeconfig: pulumi.Output[str]
    workload_provider: k8s.Provider
    azure_cloud_provider_chart_version: pulumi.Output[str]
    azure_cloud_provider_status: pulumi.Output[object]
    calico_chart_version: pulumi.Output[str]
    calico_status: pulumi.Output[object]
    workload_cluster_ready: pulumi.Output[bool]

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        subscription_id: str,
        tenant_id: pulumi.Input[str],
        client_id: pulumi.Input[str],
        node_identity_resource_id: pulumi.Input[str],
        identity_name: pulumi.Input[str],
        identity_namespace: pulumi.Input[str],
        location: str,
        additional_tags: Mapping[str, str],
        byo_subnet: AzureBYOSubnet | None = None,
        kubernetes_version: str,
        node_pools: tuple[AzureBYONodePoolSpec, ...],
        provider: k8s.Provider | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:AzureBYOWorkloadClusterInfrastructure",
            name,
            props={},
            opts=opts,
        )

        controller_node, worker_nodes = _partition_node_pools(node_pools)

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

        if byo_subnet is None:
            raise ValueError(
                "azure-byo requires an explicit or auto-discovered VNet/subnet"
            )

        cluster_name = _cluster_name(instance)
        control_plane_name = _resource_name(instance, controller_node.name)
        node_identity_provider_id = pulumi.Output.concat(
            _AZURE_PROVIDER_ID_PREFIX,
            node_identity_resource_id,
        )

        def child_options(
            *,
            depends_on: list[pulumi.Input[pulumi.Resource]] | None = None,
        ) -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=depends_on,
            )

        azure_cluster = k8s.apiextensions.CustomResource(
            "azure-cluster",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind="AzureCluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": foreground_delete_annotations(),
            },
            spec=_azure_cluster_spec(
                cluster_name=cluster_name,
                identity_name=identity_name,
                identity_namespace=identity_namespace,
                location=location,
                resource_group=resource_group.name,
                subscription_id=subscription_id,
                subnet=byo_subnet,
                additional_tags=additional_tags,
            ),
            opts=child_options(depends_on=[resource_group]),
        )
        cluster = k8s.apiextensions.CustomResource(
            "cluster",
            api_version=_CAPI_API_VERSION,
            kind="Cluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": foreground_delete_annotations(
                    {PULUMI_SKIP_AWAIT_ANNOTATION: "true"}
                ),
                "labels": {"cloud-provider": "azure"},
            },
            spec=_cluster_spec(
                cluster_name=cluster_name,
                control_plane_name=control_plane_name,
            ),
            opts=child_options(depends_on=[azure_cluster]),
        )
        azure_cluster_ready = k8s.apiextensions.CustomResourcePatch(
            "azure-cluster-ready",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind="AzureCluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": pulumi_wait_for("condition=Ready"),
            },
            opts=child_options(depends_on=[cluster]),
        )
        control_plane_machine_template = k8s.apiextensions.CustomResource(
            "control-plane-machine-template",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind="AzureMachineTemplate",
            metadata={"name": control_plane_name, "namespace": _NAMESPACE},
            spec=_machine_template_spec(
                node=controller_node,
                subnet_name=byo_subnet.subnet_name,
                node_identity_provider_id=node_identity_provider_id,
                additional_tags=additional_tags,
            ),
            opts=child_options(depends_on=[azure_cluster_ready]),
        )
        control_plane = k8s.apiextensions.CustomResource(
            "control-plane",
            api_version=_CONTROL_PLANE_API_VERSION,
            kind="KubeadmControlPlane",
            metadata={
                "name": control_plane_name,
                "namespace": _NAMESPACE,
                "annotations": foreground_delete_annotations(
                    pulumi_wait_for("condition=Initialized")
                ),
            },
            spec=_kubeadm_control_plane_spec(
                node=controller_node,
                cluster_name=cluster_name,
                control_plane_name=control_plane_name,
                kubernetes_version=kubernetes_version,
            ),
            opts=child_options(
                depends_on=[
                    cluster,
                    azure_cluster_ready,
                    control_plane_machine_template,
                ]
            ),
        )
        worker_machine_deployments: list[k8s.apiextensions.CustomResource] = []
        worker_names: list[str] = []
        for worker_node in worker_nodes:
            worker_name = _resource_name(instance, worker_node.name)
            worker_names.append(worker_name)
            template_dependencies: list[pulumi.Input[pulumi.Resource]] = [
                azure_cluster_ready
            ]
            if worker_node.attach_to_flex:
                template_dependencies.append(flex)
            worker_machine_template = k8s.apiextensions.CustomResource(
                f"{worker_node.name}-machine-template",
                api_version=_INFRASTRUCTURE_API_VERSION,
                kind="AzureMachineTemplate",
                metadata={"name": worker_name, "namespace": _NAMESPACE},
                spec=_machine_template_spec(
                    node=worker_node,
                    subnet_name=byo_subnet.subnet_name,
                    node_identity_provider_id=node_identity_provider_id,
                    virtual_machine_scale_set_id=(
                        flex.id if worker_node.attach_to_flex else None
                    ),
                    additional_tags=additional_tags,
                ),
                opts=child_options(depends_on=template_dependencies),
            )
            worker_bootstrap_template = k8s.apiextensions.CustomResource(
                f"{worker_node.name}-bootstrap-template",
                api_version=_BOOTSTRAP_API_VERSION,
                kind="KubeadmConfigTemplate",
                metadata={"name": worker_name, "namespace": _NAMESPACE},
                spec=_kubeadm_config_template_spec(
                    node=worker_node,
                    worker_name=worker_name,
                ),
                opts=child_options(depends_on=[cluster, azure_cluster_ready]),
            )
            worker_machine_deployment = k8s.apiextensions.CustomResource(
                f"{worker_node.name}-machine-deployment",
                api_version=_CAPI_API_VERSION,
                kind="MachineDeployment",
                metadata={
                    "name": worker_name,
                    "namespace": _NAMESPACE,
                    "annotations": foreground_delete_annotations(),
                },
                spec=_machine_deployment_spec(
                    node=worker_node,
                    cluster_name=cluster_name,
                    worker_name=worker_name,
                    kubernetes_version=kubernetes_version,
                ),
                opts=child_options(
                    depends_on=[
                        cluster,
                        control_plane,
                        worker_machine_template,
                        worker_bootstrap_template,
                    ]
                ),
            )
            worker_machine_deployments.append(worker_machine_deployment)
        workload_kubeconfig_secret = k8s.core.v1.Secret.get(
            "workload-kubeconfig-secret",
            id=f"{_NAMESPACE}/{cluster_name}-kubeconfig",
            opts=child_options(depends_on=[control_plane]),
        )
        workload_kubeconfig = pulumi.Output.secret(
            workload_kubeconfig_secret.data.apply(
                lambda data: _decode_secret_data_value(
                    data,
                    _WORKLOAD_KUBECONFIG_SECRET_KEY,
                )
            )
        )
        workload_provider = k8s.Provider(
            "workload-k8s",
            kubeconfig=workload_kubeconfig,
            upsert_existing_objects=True,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[workload_kubeconfig_secret],
            ),
        )
        azure_cloud_provider = AzureCloudProvider(
            "azure-cloud-provider",
            cluster_name=cluster_name,
            pod_cidr=_POD_CIDR,
            provider=workload_provider,
            depends_on=[workload_kubeconfig_secret],
            opts=pulumi.ResourceOptions(parent=self),
        )
        calico = CalicoVXLAN(
            "calico-vxlan",
            pod_cidr=_POD_CIDR,
            provider=workload_provider,
            depends_on=[azure_cloud_provider],
            opts=pulumi.ResourceOptions(parent=self),
        )
        cluster_control_plane_available = k8s.apiextensions.CustomResourcePatch(
            "cluster-control-plane-available",
            api_version=_CAPI_API_VERSION,
            kind="Cluster",
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": pulumi_wait_for(_WAIT_FOR_CONTROL_PLANE_AVAILABLE),
            },
            opts=child_options(
                depends_on=[azure_cloud_provider, calico],
            ),
        )
        worker_available = [
            k8s.apiextensions.CustomResourcePatch(
                f"{worker_node.name}-machine-deployment-available",
                api_version=_CAPI_API_VERSION,
                kind="MachineDeployment",
                metadata={
                    "name": worker_name,
                    "namespace": _NAMESPACE,
                    "annotations": pulumi_wait_for("condition=Available"),
                },
                opts=child_options(
                    depends_on=[
                        worker_machine_deployment,
                        azure_cloud_provider,
                        calico,
                    ]
                ),
            )
            for worker_node, worker_name, worker_machine_deployment in zip(
                worker_nodes,
                worker_names,
                worker_machine_deployments,
                strict=True,
            )
        ]

        self.resource_group_id = resource_group.id
        self.resource_group_name = resource_group.name
        self.vmss_flex_id = flex.id
        self.vmss_flex_name = flex.name
        self.byo_subnet = byo_subnet
        self.cluster_name = pulumi.Output.from_input(cluster_name)
        self.control_plane_name = pulumi.Output.from_input(control_plane_name)
        self.worker_machine_deployment_names = [
            pulumi.Output.from_input(worker_name) for worker_name in worker_names
        ]
        self.worker_machine_deployment_name = self.worker_machine_deployment_names[0]
        self.control_plane_ready = cluster_control_plane_available.id.apply(
            lambda _: True
        )
        self.workload_kubeconfig = workload_kubeconfig
        self.workload_provider = workload_provider
        self.azure_cloud_provider_chart_version = azure_cloud_provider.chart_version
        self.azure_cloud_provider_status = azure_cloud_provider.status
        self.calico_chart_version = calico.chart_version
        self.calico_status = calico.status
        self.workload_cluster_ready = pulumi.Output.all(
            *[worker.id for worker in worker_available]
        ).apply(lambda _: True)
        self.register_outputs(
            {
                "resource_group_id": self.resource_group_id,
                "resource_group_name": self.resource_group_name,
                "vmss_flex_id": self.vmss_flex_id,
                "vmss_flex_name": self.vmss_flex_name,
                "byo_subnet": (
                    self.byo_subnet.model_dump() if self.byo_subnet is not None else None
                ),
                "cluster_name": self.cluster_name,
                "control_plane_name": self.control_plane_name,
                "worker_machine_deployment_name": self.worker_machine_deployment_name,
                "worker_machine_deployment_names": (
                    self.worker_machine_deployment_names
                ),
                "control_plane_ready": self.control_plane_ready,
                "azure_cloud_provider_chart_version": (
                    self.azure_cloud_provider_chart_version
                ),
                "azure_cloud_provider_status": self.azure_cloud_provider_status,
                "calico_chart_version": self.calico_chart_version,
                "calico_status": self.calico_status,
                "workload_cluster_ready": self.workload_cluster_ready,
            }
        )
