"""Azure BYO workload-class config and empty Flex VMSS tests."""

from __future__ import annotations

from typing import Any, cast

import pytest

from localenv import AzureHostNetwork, AzureResourcePlacement
import stacks.workload_cluster.workload_cluster_class_azure_byo as azure_byo_module
from stacks.workload_cluster.workload_cluster_class_azure_byo import (
    AzureBYOSubnetConfig,
    AzureBYOVNetConfig,
    AzureBYOWorkloadClusterConfig,
    AzureBYOWorkloadSpec,
    _default_node_pools,
    _explicit_byo_subnet,
    _resolve_byo_subnet,
    _resolve_resource_group,
)
from stacks.workload_cluster.workload_cluster_infrastructure_azure_byo import (
    _CONTROL_PLANE_READY_API_VERSION,
    _WAIT_FOR_CONTROL_PLANE_AVAILABLE,
    AzureBYOMarketplaceImage,
    AzureBYONodePoolSpec,
    AzureBYOSubnet,
    _autoscaler_annotations,
    _cluster_annotations,
    _control_plane_annotations,
    _control_plane_ready_annotations,
    _azure_cluster_spec,
    _cluster_spec,
    _effective_tags,
    _kubeadm_config_template_spec,
    _kubeadm_control_plane_spec,
    _machine_deployment_spec,
    _machine_template_spec,
    _partition_node_pools,
    _resource_name,
    _resource_group_args,
    _resource_group_name,
    _ssh_users,
    _validate_node_pool_names,
    _vmss_flex_args,
    _vmss_flex_name,
)
from stacks.kubernetes_annotations import (
    DELETE_PROPAGATION_FOREGROUND,
    PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION,
    PULUMI_SKIP_AWAIT_ANNOTATION,
    PULUMI_WAIT_FOR_ANNOTATION,
)


_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"
_OTHER_SUBSCRIPTION_ID = "55555555-5555-5555-5555-555555555555"


def _controller_node() -> AzureBYONodePoolSpec:
    return AzureBYONodePoolSpec(
        name="control-plane",
        node_type="controller",
        vm_size="Standard_D2as_v5",
        replicas=1,
        controller=True,
    )


def _compute_node() -> AzureBYONodePoolSpec:
    return AzureBYONodePoolSpec(
        name="compute",
        node_type="compute",
        vm_size="Standard_D2as_v5",
        replicas=1,
        attach_to_flex=True,
        autoscaler_bounds=(1, 10),
    )


def _v100_image() -> AzureBYOMarketplaceImage:
    return AzureBYOMarketplaceImage(
        publisher="microsoft-dsvm",
        offer="ubuntu-hpc",
        sku="2404-v100",
        version="24.04.2026052501",
    )


def test_machine_template_spec_renders_marketplace_image_for_gpu_nodes() -> None:
    node = AzureBYONodePoolSpec(
        name="compute",
        node_type="compute",
        vm_size="Standard_ND40rs_v2",
        replicas=2,
        attach_to_flex=True,
        image=_v100_image(),
    )
    spec = _machine_template_spec(
        node=node,
        subnet_name="node-subnet",
        node_identity_provider_id="azure:///subscriptions/x/uami",
        additional_tags={"Owner": "t-hernandezc"},
        virtual_machine_scale_set_id="/subscriptions/x/vmss/flex",
    )
    template_spec = cast(dict[str, Any], spec["template"])["spec"]
    assert template_spec["image"] == {
        "marketplace": {
            "publisher": "microsoft-dsvm",
            "offer": "ubuntu-hpc",
            "sku": "2404-v100",
            "version": "24.04.2026052501",
        }
    }
    assert template_spec["vmSize"] == "Standard_ND40rs_v2"
    assert template_spec["virtualMachineScaleSetID"] == "/subscriptions/x/vmss/flex"


def test_machine_template_spec_omits_image_when_unset() -> None:
    spec = _machine_template_spec(
        node=_compute_node(),
        subnet_name="node-subnet",
        node_identity_provider_id="azure:///subscriptions/x/uami",
        additional_tags={},
    )
    template_spec = cast(dict[str, Any], spec["template"])["spec"]
    assert "image" not in template_spec


def test_default_node_pools_applies_worker_image_to_compute_only() -> None:
    image = _v100_image()
    parameters = AzureBYOWorkloadSpec(
        subscription_id=_SUBSCRIPTION_ID,
        location="southcentralus",
        worker_vm_size="Standard_ND40rs_v2",
        worker_replicas=2,
        worker_image=image,
    )
    controller, workers = _partition_node_pools(_default_node_pools(parameters))
    assert controller.image is None
    assert len(workers) == 1
    assert workers[0].image == image
    assert workers[0].vm_size == "Standard_ND40rs_v2"


def test_cluster_lifecycle_annotations_defer_readiness_to_late_patch() -> None:
    assert _cluster_annotations() == {
        PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION: DELETE_PROPAGATION_FOREGROUND,
        PULUMI_SKIP_AWAIT_ANNOTATION: "true",
    }
    assert _WAIT_FOR_CONTROL_PLANE_AVAILABLE == "condition=ControlPlaneReady"


def test_control_plane_creation_defers_readiness_to_v1beta2_patch() -> None:
    assert _CONTROL_PLANE_READY_API_VERSION == (
        "controlplane.cluster.x-k8s.io/v1beta2"
    )
    assert _control_plane_annotations() == {
        PULUMI_DELETION_PROPAGATION_POLICY_ANNOTATION: DELETE_PROPAGATION_FOREGROUND,
        PULUMI_SKIP_AWAIT_ANNOTATION: "true",
    }
    assert _control_plane_ready_annotations() == {
        PULUMI_WAIT_FOR_ANNOTATION: "condition=Initialized",
    }


@pytest.fixture(autouse=True)
def _mock_local_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        azure_byo_module,
        "discover_azure_resource_placement",
        lambda *, raise_on_missing=False: AzureResourcePlacement(
            subscription_id=_SUBSCRIPTION_ID,
            location="westus2",
            resource_group="host-rg",
        ),
    )


def test_azure_byo_config_does_not_expose_resource_group_or_vmss() -> None:
    config = AzureBYOWorkloadClusterConfig(
        parameters=AzureBYOWorkloadSpec(
            subscription_id=_SUBSCRIPTION_ID,
            location="southcentralus",
            additional_tags={"Owner": "zheyushen"},
        )
    )

    assert config.to_config() == {
        "className": "azure-byo",
        "parameters": {
            "subscriptionId": _SUBSCRIPTION_ID,
            "location": "southcentralus",
            "additionalTags": {"Owner": "zheyushen"},
        },
    }


def test_azure_byo_config_serializes_auto_discovered_vnet_option() -> None:
    config = AzureBYOWorkloadClusterConfig(
        parameters=AzureBYOWorkloadSpec(
            subscription_id=_SUBSCRIPTION_ID,
            location="westus2",
            use_auto_discovered_vnet=True,
        )
    )

    assert config.to_config()["parameters"]["useAutoDiscoveredVnet"] is True


def test_azure_byo_config_serializes_ssh_authorized_keys() -> None:
    config = AzureBYOWorkloadClusterConfig(
        parameters=AzureBYOWorkloadSpec.model_validate(
            {
                "subscriptionId": _SUBSCRIPTION_ID,
                "location": "westus2",
                "sshUsername": "debugger",
                "sshAuthorizedKeys": (
                    "ssh-rsa AAAAfirst first@example.invalid",
                    "ssh-ed25519 AAAAsecond second@example.invalid",
                ),
            }
        )
    )
    parameters = cast(dict[str, Any], config.to_config()["parameters"])

    assert parameters["sshUsername"] == "debugger"
    assert parameters["sshAuthorizedKeys"] == [
                "ssh-rsa AAAAfirst first@example.invalid",
                "ssh-ed25519 AAAAsecond second@example.invalid",
    ]


def test_azure_byo_config_serializes_explicit_vnet_and_subnet() -> None:
    config = AzureBYOWorkloadClusterConfig(
        parameters=AzureBYOWorkloadSpec(
            subscription_id=_SUBSCRIPTION_ID,
            location="westus2",
            vnet=AzureBYOVNetConfig(
                name="shared-vnet",
                resource_group="network-rg",
                subnet=AzureBYOSubnetConfig(
                    name="cluster-subnet",
                    address_prefix="10.42.0.0/24",
                ),
            ),
        )
    )

    assert config.to_config()["parameters"] == {
        "subscriptionId": _SUBSCRIPTION_ID,
        "additionalTags": {},
        "vnet": {
            "name": "shared-vnet",
            "resourceGroup": "network-rg",
            "subnet": {
                "name": "cluster-subnet",
                "addressPrefix": "10.42.0.0/24",
            },
        },
    }


def _host_network(
    *,
    subscription_id: str = _SUBSCRIPTION_ID,
    location: str = "westus2",
) -> AzureHostNetwork:
    return AzureHostNetwork(
        subscription_id=subscription_id,
        location=location,
        vnet_id="/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/virtualNetworks/host-vnet",
        vnet_name="host-vnet",
        vnet_resource_group="network-rg",
        subnet_id="/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/virtualNetworks/host-vnet/subnets/default",
        subnet_name="default",
        subnet_address_prefix="10.0.0.0/24",
        network_security_group_id="/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/networkSecurityGroups/host-nsg",
    )


@pytest.mark.parametrize("use_auto_discovered_vnet", [None, True])
def test_auto_discovered_vnet_resolves_byo_subnet_interface(
    monkeypatch: pytest.MonkeyPatch,
    use_auto_discovered_vnet: bool | None,
) -> None:
    monkeypatch.setattr(
        azure_byo_module,
        "discover_azure_host_network",
        lambda *, raise_on_missing=False: _host_network(),
    )

    subnet = _resolve_byo_subnet(
        AzureBYOWorkloadSpec(
            subscription_id=_SUBSCRIPTION_ID,
            location="westus2",
            use_auto_discovered_vnet=use_auto_discovered_vnet,
        )
    )

    assert subnet == AzureBYOSubnet(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet_id="/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/virtualNetworks/host-vnet",
        vnet_name="host-vnet",
        vnet_resource_group="network-rg",
        subnet_id="/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/virtualNetworks/host-vnet/subnets/default",
        subnet_name="default",
        address_prefix="10.0.0.0/24",
        network_security_group_id="/subscriptions/sub/resourceGroups/network-rg/providers/Microsoft.Network/networkSecurityGroups/host-nsg",
    )


def test_disabled_auto_discovery_does_not_resolve_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        azure_byo_module,
        "discover_azure_host_network",
        lambda **_: pytest.fail("network discovery should not run"),
    )

    assert _resolve_byo_subnet(
        AzureBYOWorkloadSpec(
            subscription_id=_SUBSCRIPTION_ID,
            location="westus2",
            use_auto_discovered_vnet=False,
        )
    ) is None


def test_explicit_vnet_and_subnet_resolve_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        azure_byo_module,
        "discover_azure_host_network",
        lambda **_: pytest.fail("explicit network should bypass discovery"),
    )
    parameters = AzureBYOWorkloadSpec(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet=AzureBYOVNetConfig(
            name="shared-vnet",
            resource_group="network-rg",
            subnet=AzureBYOSubnetConfig(name="cluster-subnet"),
        ),
    )

    subnet = _resolve_byo_subnet(parameters)

    assert subnet == AzureBYOSubnet(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet_id=(
            f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/network-rg/"
            "providers/Microsoft.Network/virtualNetworks/shared-vnet"
        ),
        vnet_name="shared-vnet",
        vnet_resource_group="network-rg",
        subnet_id=(
            f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/network-rg/"
            "providers/Microsoft.Network/virtualNetworks/shared-vnet/"
            "subnets/cluster-subnet"
        ),
        subnet_name="cluster-subnet",
    )
    assert _explicit_byo_subnet(parameters) == subnet


def test_true_auto_discovery_overrides_explicit_vnet_and_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        azure_byo_module,
        "discover_azure_host_network",
        lambda *, raise_on_missing=False: _host_network(),
    )
    parameters = AzureBYOWorkloadSpec(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet=AzureBYOVNetConfig(
            name="ignored-vnet",
            resource_group="ignored-rg",
            subnet=AzureBYOSubnetConfig(name="ignored-subnet"),
        ),
        use_auto_discovered_vnet=True,
    )

    subnet = _resolve_byo_subnet(parameters)

    assert subnet is not None
    assert subnet.vnet_name == "host-vnet"
    assert subnet.subnet_name == "default"


def test_false_auto_discovery_uses_explicit_vnet_and_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        azure_byo_module,
        "discover_azure_host_network",
        lambda **_: pytest.fail("false should disable discovery"),
    )
    parameters = AzureBYOWorkloadSpec(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet=AzureBYOVNetConfig(
            name="shared-vnet",
            resource_group="network-rg",
            subnet=AzureBYOSubnetConfig(name="cluster-subnet"),
        ),
        use_auto_discovered_vnet=False,
    )

    subnet = _resolve_byo_subnet(parameters)

    assert subnet is not None
    assert subnet.vnet_name == "shared-vnet"
    assert subnet.subnet_name == "cluster-subnet"


def test_explicit_vnet_requires_nested_subnet() -> None:
    with pytest.raises(ValueError, match="subnet"):
        AzureBYOWorkloadSpec.model_validate(
            {
                "subscriptionId": _SUBSCRIPTION_ID,
                "location": "westus2",
                "vnet": {
                    "name": "shared-vnet",
                    "resourceGroup": "network-rg",
                },
            }
        )


@pytest.mark.parametrize(
    ("network", "message"),
    [
        (_host_network(subscription_id=_OTHER_SUBSCRIPTION_ID), "subscription"),
        (_host_network(location="eastus2"), "location"),
    ],
)
def test_auto_discovered_vnet_must_match_workload_placement(
    monkeypatch: pytest.MonkeyPatch,
    network: AzureHostNetwork,
    message: str,
) -> None:
    monkeypatch.setattr(
        azure_byo_module,
        "discover_azure_host_network",
        lambda *, raise_on_missing=False: network,
    )

    with pytest.raises(ValueError, match=message):
        _resolve_byo_subnet(
            AzureBYOWorkloadSpec(
                subscription_id=_SUBSCRIPTION_ID,
                location="westus2",
                use_auto_discovered_vnet=True,
            )
        )


def test_resource_group_args_derive_owned_resource_group() -> None:
    args = _resource_group_args(
        instance="caps-self",
        location="southcentralus",
        additional_tags={"Owner": "zheyushen"},
    )

    assert args.resource_group_name == "caps-self-rg"
    assert args.location == "southcentralus"
    assert args.tags == {"Owner": "zheyushen"}


def test_azure_byo_config_serializes_discovered_resource_group_option() -> None:
    config = AzureBYOWorkloadClusterConfig(
        parameters=AzureBYOWorkloadSpec.model_validate(
            {
                "subscriptionId": _SUBSCRIPTION_ID,
                "location": "westus2",
                "useDiscoveredResourceGroup": True,
            }
        )
    )
    parameters = cast(dict[str, Any], config.to_config()["parameters"])

    assert parameters["useDiscoveredResourceGroup"] is True


def test_resolve_resource_group_owns_new_group_by_default() -> None:
    parameters = AzureBYOWorkloadSpec.model_validate(
        {"subscriptionId": _SUBSCRIPTION_ID, "location": "westus2"}
    )

    assert _resolve_resource_group(parameters) is None


def test_resolve_resource_group_uses_discovered_host_group() -> None:
    parameters = AzureBYOWorkloadSpec.model_validate(
        {
            "subscriptionId": _SUBSCRIPTION_ID,
            "location": "westus2",
            "useDiscoveredResourceGroup": True,
        }
    )

    assert _resolve_resource_group(parameters) == "host-rg"


def test_resolve_resource_group_rejects_subscription_mismatch() -> None:
    parameters = AzureBYOWorkloadSpec.model_validate(
        {
            "subscriptionId": _OTHER_SUBSCRIPTION_ID,
            "location": "westus2",
            "useDiscoveredResourceGroup": True,
        }
    )

    with pytest.raises(ValueError, match="subscription"):
        _resolve_resource_group(parameters)


def test_resolve_resource_group_allows_location_mismatch() -> None:
    parameters = AzureBYOWorkloadSpec.model_validate(
        {
            "subscriptionId": _SUBSCRIPTION_ID,
            "location": "eastus",
            "useDiscoveredResourceGroup": True,
        }
    )

    assert _resolve_resource_group(parameters) == "host-rg"


def test_default_node_pools_match_minimum_cluster_sizing() -> None:
    parameters = AzureBYOWorkloadSpec(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        control_plane_vm_size="Standard_D4as_v5",
        worker_vm_size="Standard_D8as_v5",
        worker_replicas=2,
    )

    controller, compute = _default_node_pools(parameters)

    assert controller == AzureBYONodePoolSpec(
        name="control-plane",
        node_type="controller",
        vm_size="Standard_D4as_v5",
        replicas=1,
        controller=True,
    )
    assert compute == AzureBYONodePoolSpec(
        name="compute",
        node_type="compute",
        vm_size="Standard_D8as_v5",
        replicas=2,
        attach_to_flex=True,
        autoscaler_bounds=(1, 10),
    )


def test_byo_autoscaler_annotations_define_worker_bounds() -> None:
    assert _autoscaler_annotations((1, 10)) == {
        "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size": "1",
        "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size": "10",
    }
    assert _autoscaler_annotations(None) == {}
    with pytest.raises(ValueError, match="minimum replicas"):
        _autoscaler_annotations((10, 1))


def test_node_pools_require_one_controller_and_worker() -> None:
    assert _partition_node_pools((_controller_node(), _compute_node())) == (
        _controller_node(),
        (_compute_node(),),
    )
    with pytest.raises(ValueError, match="exactly one controller"):
        _partition_node_pools((_compute_node(),))
    with pytest.raises(ValueError, match="at least one worker"):
        _partition_node_pools((_controller_node(),))


def test_node_pool_names_must_be_unique_after_normalization() -> None:
    nodes = (
        _controller_node(),
        AzureBYONodePoolSpec(
            name="compute_pool",
            node_type="compute",
            vm_size="Standard_D2as_v5",
            replicas=1,
        ),
        AzureBYONodePoolSpec(
            name="compute-pool",
            node_type="compute",
            vm_size="Standard_D2as_v5",
            replicas=1,
        ),
    )

    with pytest.raises(ValueError, match="after normalization"):
        _validate_node_pool_names("caps-self", nodes)


def test_node_pool_name_must_fit_dns_resource_suffix() -> None:
    node = AzureBYONodePoolSpec(
        name="n" * 63,
        node_type="compute",
        vm_size="Standard_D2as_v5",
        replicas=1,
    )

    with pytest.raises(ValueError, match="shorter than 63"):
        _validate_node_pool_names("caps-self", (_controller_node(), node))


def test_vmss_flex_args_describe_empty_placement_container() -> None:
    args = _vmss_flex_args(
        instance="caps-self",
        location="southcentralus",
        resource_group="rg-capz-self",
        additional_tags={"Owner": "zheyushen"},
    )

    assert args.resource_group_name == "rg-capz-self"
    assert args.vm_scale_set_name == "caps-self-flex"
    assert args.location == "southcentralus"
    assert args.orchestration_mode == "Flexible"
    assert args.platform_fault_domain_count == 1
    assert args.zones == ["1"]
    assert args.sku is None
    assert args.tags == {"Owner": "zheyushen"}


def test_owned_resource_names_derive_from_instance() -> None:
    assert _resource_group_name("Caps_Self") == "caps-self-rg"
    assert _vmss_flex_name("Caps_Self") == "caps-self-flex"


def test_resource_name_rejects_empty_instance() -> None:
    with pytest.raises(ValueError, match="at least one alphanumeric"):
        _resource_name("---", "flex")


def test_cluster_spec_wires_self_managed_refs_and_networks() -> None:
    assert _cluster_spec(
        cluster_name="caps-self",
        control_plane_name="caps-self-control-plane",
    ) == {
        "clusterNetwork": {
            "pods": {"cidrBlocks": ["192.168.0.0/16"]},
            "services": {"cidrBlocks": ["10.96.0.0/12"]},
            "serviceDomain": "cluster.local",
        },
        "controlPlaneRef": {
            "apiVersion": "controlplane.cluster.x-k8s.io/v1beta1",
            "kind": "KubeadmControlPlane",
            "name": "caps-self-control-plane",
        },
        "infrastructureRef": {
            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
            "kind": "AzureCluster",
            "name": "caps-self",
        },
    }


def test_azure_cluster_spec_reuses_cluster_subnet_and_internal_lb() -> None:
    subnet = AzureBYOSubnet(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet_id="/vnet",
        vnet_name="host-vnet",
        vnet_resource_group="network-rg",
        subnet_id="/vnet/subnets/default",
        subnet_name="default",
        address_prefix="10.0.0.0/24",
        network_security_group_id="/networkSecurityGroups/host-nsg",
        nat_gateway_id="/natGateways/host-nat",
        route_table_id="/routeTables/host-routes",
    )

    spec = _azure_cluster_spec(
        cluster_name="caps-self",
        identity_name="cluster-identity",
        identity_namespace="default",
        location="westus2",
        resource_group="caps-self-rg",
        subscription_id=_SUBSCRIPTION_ID,
        subnet=subnet,
        additional_tags={"Owner": "zheyushen"},
    )

    assert spec["identityRef"] == {
        "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
        "kind": "AzureClusterIdentity",
        "name": "cluster-identity",
        "namespace": "default",
    }
    assert spec["networkSpec"] == {
        "vnet": {"name": "host-vnet", "resourceGroup": "network-rg"},
        "subnets": [
            {
                "name": "default",
                "role": "cluster",
                "securityGroup": {"name": "host-nsg"},
                "routeTable": {"name": "host-routes"},
                "natGateway": {"name": "host-nat"},
            }
        ],
        "apiServerLB": {
            "name": "caps-self-internal-lb",
            "type": "Internal",
            "availabilityZones": ["1"],
        },
        "controlPlaneOutboundLB": {"frontendIPsCount": 1},
        "nodeOutboundLB": {"frontendIPsCount": 1},
    }


def test_azure_cluster_spec_exposes_public_api_server_when_requested() -> None:
    subnet = AzureBYOSubnet(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet_id="/vnet",
        vnet_name="host-vnet",
        vnet_resource_group="network-rg",
        subnet_id="/vnet/subnets/default",
        subnet_name="default",
        network_security_group_id="/networkSecurityGroups/host-nsg",
        nat_gateway_id="/natGateways/host-nat",
        route_table_id="/routeTables/host-routes",
    )

    spec = _azure_cluster_spec(
        cluster_name="caps-self",
        identity_name="cluster-identity",
        identity_namespace="default",
        location="westus2",
        resource_group="caps-self-rg",
        subscription_id=_SUBSCRIPTION_ID,
        subnet=subnet,
        additional_tags={"Owner": "zheyushen"},
        api_server_public=True,
    )

    network_spec = cast(dict[str, Any], spec["networkSpec"])
    assert network_spec["apiServerLB"] == {
        "name": "caps-self-public-lb",
        "type": "Public",
    }


def test_effective_tags_folds_azsecpack_when_public() -> None:
    assert _effective_tags(
        {"Owner": "t-hernandezc"}, api_server_public=True
    ) == {
        "Owner": "t-hernandezc",
        "AzSecPackAutoConfigReady": "true",
    }


def test_effective_tags_unchanged_when_api_server_private() -> None:
    assert _effective_tags(
        {"Owner": "t-hernandezc"}, api_server_public=False
    ) == {"Owner": "t-hernandezc"}


def test_control_plane_public_config_round_trips() -> None:
    config = AzureBYOWorkloadClusterConfig(
        parameters=AzureBYOWorkloadSpec(
            subscription_id=_SUBSCRIPTION_ID,
            location="southcentralus",
            control_plane_public=True,
        )
    )
    assert config.to_config()["parameters"]["controlPlanePublic"] is True


def test_control_plane_public_defaults_to_private() -> None:
    config = AzureBYOWorkloadClusterConfig(
        parameters=AzureBYOWorkloadSpec(
            subscription_id=_SUBSCRIPTION_ID,
            location="southcentralus",
        )
    )
    assert "controlPlanePublic" not in config.to_config()["parameters"]


def test_ssh_users_empty_without_authorized_keys() -> None:
    assert _ssh_users(ssh_username="capi", ssh_authorized_keys=()) == []


def test_control_plane_spec_injects_ssh_keys_for_head_node() -> None:
    spec = _kubeadm_control_plane_spec(
        node=_controller_node(),
        cluster_name="caps-self",
        control_plane_name="caps-self-control-plane",
        kubernetes_version="v1.36.1",
        ssh_username="capi",
        ssh_authorized_keys=("ssh-ed25519 AAAAC3NzaC1 debug@caps",),
    )
    kubeadm = cast(dict[str, Any], spec["kubeadmConfigSpec"])
    assert kubeadm["users"] == [
        {
            "name": "capi",
            "sshAuthorizedKeys": ["ssh-ed25519 AAAAC3NzaC1 debug@caps"],
        }
    ]


def test_control_plane_spec_omits_users_without_keys() -> None:
    spec = _kubeadm_control_plane_spec(
        node=_controller_node(),
        cluster_name="caps-self",
        control_plane_name="caps-self-control-plane",
        kubernetes_version="v1.36.1",
    )
    kubeadm = cast(dict[str, Any], spec["kubeadmConfigSpec"])
    assert "users" not in kubeadm


def test_worker_config_template_injects_ssh_keys() -> None:
    spec = _kubeadm_config_template_spec(
        node=_compute_node(),
        worker_name="caps-self-md-0",
        ssh_username="capi",
        ssh_authorized_keys=("ssh-ed25519 AAAAC3NzaC1 debug@caps",),
    )
    template_spec = cast(dict[str, Any], spec["template"])["spec"]
    assert template_spec["users"] == [
        {
            "name": "capi",
            "sshAuthorizedKeys": ["ssh-ed25519 AAAAC3NzaC1 debug@caps"],
        }
    ]


def test_machine_template_attaches_uami_subnet_and_flex_vmss() -> None:
    spec = _machine_template_spec(
        node=_compute_node(),
        subnet_name="default",
        node_identity_provider_id="azure:///identity",
        virtual_machine_scale_set_id="/vmss/flex",
        additional_tags={"Owner": "zheyushen"},
    )

    assert spec == {
        "template": {
            "spec": {
                "vmSize": "Standard_D2as_v5",
                "osDisk": {
                    "diskSizeGB": 128,
                    "managedDisk": {"storageAccountType": "Premium_LRS"},
                    "osType": "Linux",
                },
                "identity": "UserAssigned",
                "userAssignedIdentities": [
                    {"providerID": "azure:///identity"}
                ],
                "networkInterfaces": [{"subnetName": "default"}],
                "additionalTags": {"Owner": "zheyushen"},
                "virtualMachineScaleSetID": "/vmss/flex",
            }
        }
    }


def test_machine_template_omits_flex_vmss_for_non_flex_node_pool() -> None:
    node = AzureBYONodePoolSpec(
        name="services",
        node_type="controller",
        vm_size="Standard_D2as_v5",
        replicas=1,
    )

    spec = _machine_template_spec(
        node=node,
        subnet_name="default",
        node_identity_provider_id="azure:///identity",
        additional_tags={},
    )

    assert "virtualMachineScaleSetID" not in spec["template"]["spec"]


def test_kubeadm_control_plane_uses_external_cloud_provider() -> None:
    spec = _kubeadm_control_plane_spec(
        node=_controller_node(),
        cluster_name="caps-self",
        control_plane_name="caps-self-control-plane",
        kubernetes_version="v1.36.1",
    )

    assert "failureDomain" not in spec["machineTemplate"]
    kubeadm = spec["kubeadmConfigSpec"]
    assert kubeadm["files"][0]["contentFrom"]["secret"] == {
        "name": "caps-self-control-plane-azure-json",
        "key": "control-plane-azure.json",
    }
    assert kubeadm["initConfiguration"]["nodeRegistration"]["kubeletExtraArgs"] == {
        "cloud-provider": "external",
        "node-labels": "slinky.slurm.net/node-type=controller",
    }
    assert "127.0.0.1 apiserver.caps-self.capz.io" in kubeadm[
        "preKubeadmCommands"
    ][0]


def test_kubeadm_control_plane_adds_ssh_authorized_keys() -> None:
    keys = (
        "ssh-rsa AAAAfirst first@example.invalid",
        "ssh-ed25519 AAAAsecond second@example.invalid",
    )

    spec = _kubeadm_control_plane_spec(
        node=_controller_node(),
        cluster_name="caps-self",
        control_plane_name="caps-self-control-plane",
        kubernetes_version="v1.36.1",
        ssh_username="debugger",
        ssh_authorized_keys=keys,
    )

    kubeadm_config_spec = cast(dict[str, Any], spec["kubeadmConfigSpec"])

    assert kubeadm_config_spec["users"] == [
        {
            "name": "debugger",
            "sshAuthorizedKeys": list(keys),
        }
    ]


def test_compute_machine_deployment_references_flex_worker_templates() -> None:
    spec = _machine_deployment_spec(
        node=_compute_node(),
        cluster_name="caps-self",
        worker_name="caps-self-compute",
        kubernetes_version="v1.36.1",
    )

    assert spec["replicas"] == 1
    assert spec["template"]["metadata"]["annotations"] == {
        "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size": "1",
        "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size": "10",
    }
    assert spec["selector"]["matchLabels"] == {
        "cluster.x-k8s.io/cluster-name": "caps-self",
        "slinky.slurm.net/node-type": "compute",
        "caps-self.worker": "compute",
    }
    assert spec["template"]["spec"]["failureDomain"] == "1"
    assert spec["template"]["metadata"]["labels"][
        "slinky.slurm.net/node-type"
    ] == "compute"
    assert spec["template"]["spec"]["infrastructureRef"] == {
        "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
        "kind": "AzureMachineTemplate",
        "name": "caps-self-compute",
    }


def test_worker_kubeadm_template_mounts_azure_config_and_labels_node() -> None:
    spec = _kubeadm_config_template_spec(
        node=_compute_node(),
        worker_name="caps-self-compute",
        ssh_username="debugger",
        ssh_authorized_keys=(
            "ssh-rsa AAAAfirst first@example.invalid",
            "ssh-ed25519 AAAAsecond second@example.invalid",
        ),
    )

    template = cast(dict[str, Any], cast(dict[str, Any], spec["template"])["spec"])
    assert template["files"][0]["contentFrom"]["secret"] == {
        "name": "caps-self-compute-azure-json",
        "key": "worker-node-azure.json",
    }
    assert template["joinConfiguration"]["nodeRegistration"]["kubeletExtraArgs"] == {
        "cloud-provider": "external",
        "node-labels": "slinky.slurm.net/node-type=compute",
    }
    assert template["users"] == [
        {
            "name": "debugger",
            "sshAuthorizedKeys": [
                "ssh-rsa AAAAfirst first@example.invalid",
                "ssh-ed25519 AAAAsecond second@example.invalid",
            ],
        }
    ]
