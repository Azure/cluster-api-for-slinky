"""Unit tests for Azure control-plane config parsing and rendering."""

from __future__ import annotations

import pytest

from stacks.control_plane.control_plane_config import (
    CONTROL_PLANE_KIND_CHILD_CONFIG_KEY,
    ControlPlaneAzureConfig,
    build_control_plane_kind_azure_child_config,
    parse_control_plane_kind_config,
)


# Real-looking GUIDs (8-4-4-4-12 hex); arbitrary values, not real
# identities. Reused across tests so each one focuses on what it's
# actually checking.
_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
_PRINCIPAL_ID = "22222222-2222-2222-2222-222222222222"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"
_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"


def _full_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "clientId": _CLIENT_ID,
        "principalId": _PRINCIPAL_ID,
        "tenantId": _TENANT_ID,
        "subscriptionId": _SUBSCRIPTION_ID,
    }
    payload.update(overrides)
    return payload


def _parse_azure_payload(payload: object) -> ControlPlaneAzureConfig:
    parsed = parse_control_plane_kind_config({"azure": payload})
    assert parsed.azure is not None
    return parsed.azure


def test_build_and_parse_control_plane_kind_round_trip_minimum_fields() -> None:
    built = build_control_plane_kind_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
    )

    assert built == {
        CONTROL_PLANE_KIND_CHILD_CONFIG_KEY: {
            "awx": {"enabled": False},
            "azure": {
                "clientId": _CLIENT_ID,
                "principalId": _PRINCIPAL_ID,
                "tenantId": _TENANT_ID,
                "subscriptionId": _SUBSCRIPTION_ID,
            },
        },
    }

    parsed = parse_control_plane_kind_config(
        built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    )
    assert parsed.enable_awx is False
    assert parsed.azure == ControlPlaneAzureConfig(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        allowed_namespaces=None,
    )


def test_build_omits_allowed_namespaces_when_none() -> None:
    built = build_control_plane_kind_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        allowed_namespaces=None,
    )

    # The CR side treats absence as "allow all"; omitting the key keeps
    # the wire shape minimal and matches what the parser expects.
    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    azure = control_plane["azure"]
    assert isinstance(azure, dict)
    assert "allowedNamespaces" not in azure


def test_build_serializes_allowed_namespaces_list() -> None:
    built = build_control_plane_kind_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        allowed_namespaces=["default", "tenant-a"],
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    assert control_plane["azure"] == {
        "clientId": _CLIENT_ID,
        "principalId": _PRINCIPAL_ID,
        "tenantId": _TENANT_ID,
        "subscriptionId": _SUBSCRIPTION_ID,
        "allowedNamespaces": ["default", "tenant-a"],
    }

    parsed = parse_control_plane_kind_config(control_plane)
    assert parsed.azure is not None
    assert parsed.azure.allowed_namespaces == ["default", "tenant-a"]


def test_parse_kind_config_without_azure_leaves_azure_none() -> None:
    parsed = parse_control_plane_kind_config(None)
    assert parsed.azure is None


def test_parse_rejects_non_mapping_payload() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        _parse_azure_payload("not-a-dict")


@pytest.mark.parametrize(
    "missing_key",
    ["clientId", "principalId", "tenantId", "subscriptionId"],
)
def test_parse_rejects_missing_guid_field(missing_key: str) -> None:
    payload = _full_payload()
    del payload[missing_key]

    with pytest.raises(
        ValueError,
        match=f"controlPlane.azure.{missing_key} must be a non-empty string",
    ):
        _parse_azure_payload(payload)


@pytest.mark.parametrize(
    "bad_guid_field",
    ["clientId", "principalId", "tenantId", "subscriptionId"],
)
def test_parse_rejects_malformed_guid(bad_guid_field: str) -> None:
    payload = _full_payload(**{bad_guid_field: "not-a-guid"})

    with pytest.raises(
        ValueError, match=f"controlPlane.azure.{bad_guid_field} must be a GUID"
    ):
        _parse_azure_payload(payload)


def test_parse_rejects_non_string_guid() -> None:
    # Numbers can sneak in if config is hand-edited; the GUID check
    # must reject them before reaching the regex.
    payload = _full_payload(clientId=12345)

    with pytest.raises(
        ValueError, match="controlPlane.azure.clientId must be a non-empty string"
    ):
        _parse_azure_payload(payload)


def test_parse_rejects_non_list_allowed_namespaces() -> None:
    payload = _full_payload(allowedNamespaces="default")

    with pytest.raises(ValueError, match="must be a list of namespace names"):
        _parse_azure_payload(payload)


def test_parse_rejects_empty_allowed_namespace_entry() -> None:
    payload = _full_payload(allowedNamespaces=["default", ""])

    with pytest.raises(
        ValueError, match=r"allowedNamespaces\[1\] must be a non-empty string"
    ):
        _parse_azure_payload(payload)


def test_parse_allows_empty_allowed_namespaces_list() -> None:
    # Empty list is semantically "no namespace may reference this
    # identity" per the upstream CRD; we shouldn't *reject* it at parse
    # time because the contract has to round-trip cleanly, but we do
    # surface it as an empty list (not None) so callers can tell.
    payload = _full_payload(allowedNamespaces=[])

    parsed = _parse_azure_payload(payload)
    assert parsed.allowed_namespaces == []


def test_skip_in_cluster_preflight_defaults_to_false() -> None:
    # ``skipInClusterPreflight`` is optional; absent in the wire shape
    # means "run the preflight" so the safe default is False.
    parsed = _parse_azure_payload(_full_payload())
    assert parsed.skip_in_cluster_preflight is False


def test_skip_in_cluster_preflight_round_trips_true() -> None:
    built = build_control_plane_kind_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        skip_in_cluster_preflight=True,
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    assert control_plane["azure"] == {
        "clientId": _CLIENT_ID,
        "principalId": _PRINCIPAL_ID,
        "tenantId": _TENANT_ID,
        "subscriptionId": _SUBSCRIPTION_ID,
        "skipInClusterPreflight": True,
    }
    parsed = parse_control_plane_kind_config(control_plane)
    assert parsed.azure is not None
    assert parsed.azure.skip_in_cluster_preflight is True


def test_build_omits_skip_in_cluster_preflight_when_false() -> None:
    # The default-False case must produce the minimal wire shape so
    # operators can flip the flag back to "use the default" by removing
    # the config key, not by setting it to false explicitly.
    built = build_control_plane_kind_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        skip_in_cluster_preflight=False,
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    azure = control_plane["azure"]
    assert isinstance(azure, dict)
    assert "skipInClusterPreflight" not in azure


def test_parse_rejects_non_bool_skip_in_cluster_preflight() -> None:
    payload = _full_payload(skipInClusterPreflight="yes")

    with pytest.raises(
        ValueError, match=r"skipInClusterPreflight must be a boolean"
    ):
        _parse_azure_payload(payload)


def test_infrastructure_providers_round_trip() -> None:
    built = build_control_plane_kind_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        infrastructure_providers=("docker", "azure"),
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    assert control_plane["azure"] == {
        "clientId": _CLIENT_ID,
        "principalId": _PRINCIPAL_ID,
        "tenantId": _TENANT_ID,
        "subscriptionId": _SUBSCRIPTION_ID,
        "infrastructureProviders": ["docker", "azure"],
    }
    parsed = parse_control_plane_kind_config(control_plane)
    assert parsed.infrastructure_providers == ("docker", "azure")


def test_parse_rejects_infrastructure_providers_without_azure() -> None:
    payload = _full_payload(infrastructureProviders=["docker"])

    with pytest.raises(ValueError, match="must include 'azure'"):
        _parse_azure_payload(payload)


def test_parse_rejects_non_list_infrastructure_providers() -> None:
    payload = _full_payload(infrastructureProviders="azure")

    with pytest.raises(ValueError, match="must be a list of provider names"):
        _parse_azure_payload(payload)
