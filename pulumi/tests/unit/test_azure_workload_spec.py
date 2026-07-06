"""Unit tests for the AzureWorkloadSpec parse/build round-trip + validation."""

from __future__ import annotations

import pytest

from localenv import AzureResourcePlacement
import stacks.workload_cluster.workload_cluster_class_aks as aks_config_module
from stacks.workload_cluster.workload_cluster_class_aks import (
    AKSWorkloadClusterConfig,
    AKSWorkloadSizingConfig,
    AzureWorkloadSpec,
    _DEFAULT_AKS_KUBERNETES_VERSION,
    _DEFAULT_AKS_NODE_COUNT,
    _DEFAULT_AKS_NODE_SKU,
)
from stacks.workload_cluster.tenants import TenantsConfig


_LOCATION = "westus2"
_RESOURCE_GROUP = "rg-capz-mi-dev2"
_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"
@pytest.fixture(autouse=True)
def _mock_local_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        aks_config_module,
        "discover_azure_resource_placement",
        lambda *, raise_on_missing=False: AzureResourcePlacement(
            subscription_id=_SUBSCRIPTION_ID,
            location=_LOCATION,
            resource_group=_RESOURCE_GROUP,
        ),
    )


def _parameters(config: dict[str, object]) -> object:
    workload_clusters = config["tenants"]["workloadClusters"]  # type: ignore[index]
    return workload_clusters["caps-aks"]["parameters"]  # type: ignore[index]


def _child_config(parameters: AzureWorkloadSpec) -> dict[str, object]:
    return {
        "tenants": TenantsConfig(
            workload_clusters={
                "caps-aks": AKSWorkloadClusterConfig(
                    parameters=parameters,
                ),
            },
        ).to_config()
    }


def test_build_and_parse_round_trip_defaults() -> None:
    built = _child_config(
        AzureWorkloadSpec(location=_LOCATION, resource_group=_RESOURCE_GROUP)
    )

    assert built == {
        "tenants": {
            "workloadClusters": {
                "caps-aks": {
                    "className": "aks",
                    "parameters": {
                        "subscriptionId": _SUBSCRIPTION_ID,
                        "location": _LOCATION,
                        "resourceGroup": _RESOURCE_GROUP,
                        "additionalTags": {},
                    },
                }
            }
        }
    }

    parsed = AzureWorkloadSpec.model_validate(_parameters(built))
    assert parsed == AzureWorkloadSpec(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        aks=AKSWorkloadSizingConfig(
            kubernetes_version=_DEFAULT_AKS_KUBERNETES_VERSION,
            node_sku=_DEFAULT_AKS_NODE_SKU,
            node_count=_DEFAULT_AKS_NODE_COUNT,
        ),
    )


def test_build_omits_aks_block_when_no_overrides() -> None:
    built = _child_config(
        AzureWorkloadSpec(location=_LOCATION, resource_group=_RESOURCE_GROUP)
    )

    # No sizing overrides => no ``aks`` key => the parser falls back to the
    # module defaults. Keeping the wire shape minimal lets an operator revert
    # to "use the default" by removing the config key.
    assert "aks" not in _parameters(built)  # type: ignore[operator]
    assert _parameters(built)["additionalTags"] == {}  # type: ignore[index]


def test_build_and_parse_round_trip_additional_tags() -> None:
    built = _child_config(
        AzureWorkloadSpec(
            location=_LOCATION,
            resource_group=_RESOURCE_GROUP,
            additional_tags={"Owner": "t-hernandezc"},
        )
    )

    assert _parameters(built) == {
        "subscriptionId": _SUBSCRIPTION_ID,
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": {"Owner": "t-hernandezc"},
    }

    parsed = AzureWorkloadSpec.model_validate(_parameters(built))
    assert parsed.additional_tags == {"Owner": "t-hernandezc"}


def test_build_omits_additional_tags_when_empty() -> None:
    built = _child_config(
        AzureWorkloadSpec(
            location=_LOCATION,
            resource_group=_RESOURCE_GROUP,
            additional_tags={},
        )
    )

    assert _parameters(built)["additionalTags"] == {}  # type: ignore[index]


def test_parse_defaults_additional_tags_to_empty() -> None:
    parsed = AzureWorkloadSpec.model_validate(
        {"location": _LOCATION, "resourceGroup": _RESOURCE_GROUP}
    )

    assert parsed.additional_tags == {}


def test_build_and_parse_round_trip_full_aks_overrides() -> None:
    built = _child_config(
        AzureWorkloadSpec(
            location=_LOCATION,
            resource_group=_RESOURCE_GROUP,
            aks=AKSWorkloadSizingConfig(
                kubernetes_version="v1.31.1",
                node_sku="Standard_D4s_v3",
                node_count=3,
            ),
        )
    )

    assert _parameters(built) == {
        "subscriptionId": _SUBSCRIPTION_ID,
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": {},
        "aks": {
            "kubernetesVersion": "v1.31.1",
            "nodeSku": "Standard_D4s_v3",
            "nodeCount": 3,
        },
    }

    parsed = AzureWorkloadSpec.model_validate(_parameters(built))
    assert parsed == AzureWorkloadSpec(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        aks=AKSWorkloadSizingConfig(
            kubernetes_version="v1.31.1",
            node_sku="Standard_D4s_v3",
            node_count=3,
        ),
    )


def test_build_omits_only_unset_aks_keys() -> None:
    built = _child_config(
        AzureWorkloadSpec(
            location=_LOCATION,
            resource_group=_RESOURCE_GROUP,
            aks=AKSWorkloadSizingConfig(node_count=5),
        )
    )

    # Only the overridden key is serialized; the other two fall back to
    # defaults on the parse side.
    assert _parameters(built) == {
        "subscriptionId": _SUBSCRIPTION_ID,
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": {},
        "aks": {"nodeCount": 5},
    }


def test_parse_applies_defaults_for_partial_aks_block() -> None:
    parsed = AzureWorkloadSpec.model_validate(
        {
            "location": _LOCATION,
            "resourceGroup": _RESOURCE_GROUP,
            "aks": {"nodeCount": 2},
        }
    )

    assert parsed.aks.node_count == 2
    assert parsed.aks.kubernetes_version == _DEFAULT_AKS_KUBERNETES_VERSION
    assert parsed.aks.node_sku == _DEFAULT_AKS_NODE_SKU


def test_parse_rejects_none_payload() -> None:
    with pytest.raises(ValueError, match="valid dictionary"):
        AzureWorkloadSpec.model_validate(None)


def test_parse_rejects_non_mapping_payload() -> None:
    with pytest.raises(ValueError, match="valid dictionary"):
        AzureWorkloadSpec.model_validate("not-a-dict")  # type: ignore[arg-type]


@pytest.mark.parametrize("omitted_key", ["subscriptionId", "location", "resourceGroup"])
def test_parse_allows_omitting_discovery_injected_placement(omitted_key: str) -> None:
    # Placement fields default from local Azure placement discovery, so any one
    # may be omitted from config.
    payload: dict[str, str] = {
        "subscriptionId": _SUBSCRIPTION_ID,
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
    }
    del payload[omitted_key]

    parsed = AzureWorkloadSpec.model_validate(payload)

    if omitted_key == "subscriptionId":
        assert str(parsed.subscription_id) == _SUBSCRIPTION_ID
        assert parsed.location == _LOCATION
        assert parsed.resource_group == _RESOURCE_GROUP
    elif omitted_key == "location":
        assert parsed.location == _LOCATION
        assert parsed.resource_group == _RESOURCE_GROUP
    else:
        assert parsed.resource_group == _RESOURCE_GROUP
        assert parsed.location == _LOCATION


def test_parse_allows_omitting_both_placement_fields() -> None:
    parsed = AzureWorkloadSpec.model_validate({})

    assert str(parsed.subscription_id) == _SUBSCRIPTION_ID
    assert parsed.location == _LOCATION
    assert parsed.resource_group == _RESOURCE_GROUP
    assert parsed.additional_tags == {}


def test_parse_rejects_non_object_aks_block() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": "big",
    }

    with pytest.raises(ValueError, match="aks"):
        AzureWorkloadSpec.model_validate(payload)


def test_parse_rejects_empty_kubernetes_version() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": {"kubernetesVersion": ""},
    }

    with pytest.raises(
        ValueError, match="kubernetesVersion"
    ):
        AzureWorkloadSpec.model_validate(payload)


@pytest.mark.parametrize("bad_count", [0, -1, "two", 1.5, True])
def test_parse_rejects_non_positive_int_node_count(bad_count: object) -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": {"nodeCount": bad_count},
    }

    with pytest.raises(ValueError, match="nodeCount"):
        AzureWorkloadSpec.model_validate(payload)


def test_parse_rejects_non_object_additional_tags() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": "Owner=me",
    }

    with pytest.raises(
        ValueError, match="additionalTags"
    ):
        AzureWorkloadSpec.model_validate(payload)


def test_parse_rejects_non_string_tag_value() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": {"Owner": 123},
    }

    with pytest.raises(
        ValueError, match="additionalTags.Owner"
    ):
        AzureWorkloadSpec.model_validate(payload)
