"""Azure BYO workload-class config and empty Flex VMSS tests."""

from __future__ import annotations

import pytest

from localenv import AzureResourcePlacement
import stacks.workload_cluster.workload_cluster_class_azure_byo as azure_byo_module
from stacks.workload_cluster.workload_cluster_class_azure_byo import (
    AzureBYOWorkloadClusterConfig,
    AzureBYOWorkloadSpec,
)
from stacks.workload_cluster.workload_cluster_infrastructure_azure_byo import (
    _resource_name,
    _resource_group_args,
    _resource_group_name,
    _vmss_flex_args,
    _vmss_flex_name,
)


_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"


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
