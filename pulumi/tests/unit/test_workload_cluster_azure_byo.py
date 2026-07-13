"""Azure BYO workload-class config and empty Flex VMSS tests."""

from __future__ import annotations

import pytest

from localenv import AzureHostNetwork, AzureResourcePlacement
import stacks.workload_cluster.workload_cluster_class_azure_byo as azure_byo_module
from stacks.workload_cluster.workload_cluster_class_azure_byo import (
    AzureBYOSubnetConfig,
    AzureBYOVNetConfig,
    AzureBYOWorkloadClusterConfig,
    AzureBYOWorkloadSpec,
    _explicit_byo_subnet,
    _resolve_byo_subnet,
)
from stacks.workload_cluster.workload_cluster_infrastructure_azure_byo import (
    AzureBYOSubnet,
    _resource_name,
    _resource_group_args,
    _resource_group_name,
    _vmss_flex_args,
    _vmss_flex_name,
)


_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"
_OTHER_SUBSCRIPTION_ID = "55555555-5555-5555-5555-555555555555"


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
    assert args.sku.capacity == 0
    assert args.tags == {"Owner": "zheyushen"}


def test_owned_resource_names_derive_from_instance() -> None:
    assert _resource_group_name("Caps_Self") == "caps-self-rg"
    assert _vmss_flex_name("Caps_Self") == "caps-self-flex"


def test_resource_name_rejects_empty_instance() -> None:
    with pytest.raises(ValueError, match="at least one alphanumeric"):
        _resource_name("---", "flex")
