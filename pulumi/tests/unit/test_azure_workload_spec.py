"""Unit tests for the AzureWorkloadSpec parse/build round-trip + validation."""

from __future__ import annotations

import pytest

from stacks.workload_cluster.tenants_azure import (
    AZURE_WORKLOAD_CHILD_CONFIG_KEY,
    AzureWorkloadSpec,
    _DEFAULT_AKS_KUBERNETES_VERSION,
    _DEFAULT_AKS_NODE_COUNT,
    _DEFAULT_AKS_NODE_SKU,
    build_azure_workload_child_config,
    parse_azure_workload_spec,
)


_LOCATION = "westus2"
_RESOURCE_GROUP = "rg-capz-mi-dev2"


def test_build_and_parse_round_trip_defaults() -> None:
    built = build_azure_workload_child_config(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
    )

    assert built == {
        AZURE_WORKLOAD_CHILD_CONFIG_KEY: {
            "location": _LOCATION,
            "resourceGroup": _RESOURCE_GROUP,
        }
    }

    parsed = parse_azure_workload_spec(built[AZURE_WORKLOAD_CHILD_CONFIG_KEY])
    assert parsed == AzureWorkloadSpec(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        kubernetes_version=_DEFAULT_AKS_KUBERNETES_VERSION,
        node_sku=_DEFAULT_AKS_NODE_SKU,
        node_count=_DEFAULT_AKS_NODE_COUNT,
    )


def test_build_omits_aks_block_when_no_overrides() -> None:
    built = build_azure_workload_child_config(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
    )

    # No sizing overrides => no ``aks`` key => the parser falls back to the
    # module defaults. Keeping the wire shape minimal lets an operator revert
    # to "use the default" by removing the config key.
    assert "aks" not in built[AZURE_WORKLOAD_CHILD_CONFIG_KEY]  # type: ignore[operator]
    # Likewise, no tags => no ``additionalTags`` key.
    assert "additionalTags" not in built[AZURE_WORKLOAD_CHILD_CONFIG_KEY]  # type: ignore[operator]


def test_build_and_parse_round_trip_additional_tags() -> None:
    built = build_azure_workload_child_config(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        additional_tags={"Owner": "t-hernandezc"},
    )

    assert built[AZURE_WORKLOAD_CHILD_CONFIG_KEY] == {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": {"Owner": "t-hernandezc"},
    }

    parsed = parse_azure_workload_spec(built[AZURE_WORKLOAD_CHILD_CONFIG_KEY])
    assert parsed.additional_tags == {"Owner": "t-hernandezc"}


def test_build_omits_additional_tags_when_empty() -> None:
    built = build_azure_workload_child_config(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        additional_tags={},
    )

    assert "additionalTags" not in built[AZURE_WORKLOAD_CHILD_CONFIG_KEY]  # type: ignore[operator]


def test_parse_defaults_additional_tags_to_empty() -> None:
    parsed = parse_azure_workload_spec(
        {"location": _LOCATION, "resourceGroup": _RESOURCE_GROUP}
    )

    assert parsed.additional_tags == {}


def test_build_and_parse_round_trip_full_aks_overrides() -> None:
    built = build_azure_workload_child_config(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        aks_kubernetes_version="v1.31.1",
        aks_node_sku="Standard_D4s_v3",
        aks_node_count=3,
    )

    assert built[AZURE_WORKLOAD_CHILD_CONFIG_KEY] == {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": {
            "kubernetesVersion": "v1.31.1",
            "nodeSku": "Standard_D4s_v3",
            "nodeCount": 3,
        },
    }

    parsed = parse_azure_workload_spec(built[AZURE_WORKLOAD_CHILD_CONFIG_KEY])
    assert parsed == AzureWorkloadSpec(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        kubernetes_version="v1.31.1",
        node_sku="Standard_D4s_v3",
        node_count=3,
    )


def test_build_omits_only_unset_aks_keys() -> None:
    built = build_azure_workload_child_config(
        location=_LOCATION,
        resource_group=_RESOURCE_GROUP,
        aks_node_count=5,
    )

    # Only the overridden key is serialized; the other two fall back to
    # defaults on the parse side.
    assert built[AZURE_WORKLOAD_CHILD_CONFIG_KEY] == {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": {"nodeCount": 5},
    }


def test_parse_applies_defaults_for_partial_aks_block() -> None:
    parsed = parse_azure_workload_spec(
        {
            "location": _LOCATION,
            "resourceGroup": _RESOURCE_GROUP,
            "aks": {"nodeCount": 2},
        }
    )

    assert parsed.node_count == 2
    assert parsed.kubernetes_version == _DEFAULT_AKS_KUBERNETES_VERSION
    assert parsed.node_sku == _DEFAULT_AKS_NODE_SKU


def test_parse_rejects_none_payload() -> None:
    with pytest.raises(ValueError, match="missing required Azure workload"):
        parse_azure_workload_spec(None)


def test_parse_rejects_non_mapping_payload() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        parse_azure_workload_spec("not-a-dict")  # type: ignore[arg-type]


@pytest.mark.parametrize("missing_key", ["location", "resourceGroup"])
def test_parse_rejects_missing_required_key(missing_key: str) -> None:
    payload: dict[str, str] = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
    }
    del payload[missing_key]

    with pytest.raises(
        ValueError,
        match=f"azureWorkload.{missing_key} must be a non-empty string",
    ):
        parse_azure_workload_spec(payload)


def test_parse_rejects_non_object_aks_block() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": "big",
    }

    with pytest.raises(ValueError, match="azureWorkload.aks must be an object"):
        parse_azure_workload_spec(payload)


def test_parse_rejects_empty_kubernetes_version() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": {"kubernetesVersion": ""},
    }

    with pytest.raises(
        ValueError, match="kubernetesVersion must be a non-empty string"
    ):
        parse_azure_workload_spec(payload)


@pytest.mark.parametrize("bad_count", [0, -1, "two", 1.5, True])
def test_parse_rejects_non_positive_int_node_count(bad_count: object) -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "aks": {"nodeCount": bad_count},
    }

    with pytest.raises(ValueError, match="nodeCount must be an integer >= 1"):
        parse_azure_workload_spec(payload)


def test_parse_rejects_non_object_additional_tags() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": "Owner=me",
    }

    with pytest.raises(
        ValueError, match="additionalTags must be an object of string tags"
    ):
        parse_azure_workload_spec(payload)


def test_parse_rejects_non_string_tag_value() -> None:
    payload = {
        "location": _LOCATION,
        "resourceGroup": _RESOURCE_GROUP,
        "additionalTags": {"Owner": 123},
    }

    with pytest.raises(
        ValueError, match="additionalTags.Owner must be a string"
    ):
        parse_azure_workload_spec(payload)
