"""Unit tests for the ControlPlaneAzure spec round-trip + GUID/allow-list validation."""

from __future__ import annotations

import pytest

from stacks.control_plane.control_plane_azure import (
    CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY,
    ControlPlaneAzureSpec,
    build_control_plane_azure_child_config,
    parse_control_plane_azure_spec,
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


def test_build_and_parse_round_trip_minimum_fields() -> None:
    built = build_control_plane_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
    )

    assert built == {
        CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY: {
            "clientId": _CLIENT_ID,
            "principalId": _PRINCIPAL_ID,
            "tenantId": _TENANT_ID,
            "subscriptionId": _SUBSCRIPTION_ID,
        },
    }

    parsed = parse_control_plane_azure_spec(
        built[CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY]
    )
    assert parsed == ControlPlaneAzureSpec(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        allowed_namespaces=None,
    )


def test_build_omits_allowed_namespaces_when_none() -> None:
    built = build_control_plane_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        allowed_namespaces=None,
    )

    # The CR side treats absence as "allow all"; omitting the key keeps
    # the wire shape minimal and matches what the parser expects.
    assert "allowedNamespaces" not in built[CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY]  # type: ignore[operator]


def test_build_serializes_allowed_namespaces_list() -> None:
    built = build_control_plane_azure_child_config(
        client_id=_CLIENT_ID,
        principal_id=_PRINCIPAL_ID,
        tenant_id=_TENANT_ID,
        subscription_id=_SUBSCRIPTION_ID,
        allowed_namespaces=["default", "tenant-a"],
    )

    assert built[CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY] == {
        "clientId": _CLIENT_ID,
        "principalId": _PRINCIPAL_ID,
        "tenantId": _TENANT_ID,
        "subscriptionId": _SUBSCRIPTION_ID,
        "allowedNamespaces": ["default", "tenant-a"],
    }

    parsed = parse_control_plane_azure_spec(
        built[CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY]
    )
    assert parsed.allowed_namespaces == ["default", "tenant-a"]


def test_parse_rejects_none_payload() -> None:
    with pytest.raises(ValueError, match="missing required Azure"):
        parse_control_plane_azure_spec(None)


def test_parse_rejects_non_mapping_payload() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        parse_control_plane_azure_spec("not-a-dict")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing_key",
    ["clientId", "principalId", "tenantId", "subscriptionId"],
)
def test_parse_rejects_missing_guid_field(missing_key: str) -> None:
    payload = _full_payload()
    del payload[missing_key]

    with pytest.raises(
        ValueError, match=f"azure.{missing_key} must be a non-empty string"
    ):
        parse_control_plane_azure_spec(payload)


@pytest.mark.parametrize(
    "bad_guid_field",
    ["clientId", "principalId", "tenantId", "subscriptionId"],
)
def test_parse_rejects_malformed_guid(bad_guid_field: str) -> None:
    payload = _full_payload(**{bad_guid_field: "not-a-guid"})

    with pytest.raises(
        ValueError, match=f"azure.{bad_guid_field} must be a GUID"
    ):
        parse_control_plane_azure_spec(payload)


def test_parse_rejects_non_string_guid() -> None:
    # Numbers can sneak in if config is hand-edited; the GUID check
    # must reject them before reaching the regex.
    payload = _full_payload(clientId=12345)

    with pytest.raises(ValueError, match="azure.clientId must be a non-empty string"):
        parse_control_plane_azure_spec(payload)


def test_parse_rejects_non_list_allowed_namespaces() -> None:
    payload = _full_payload(allowedNamespaces="default")

    with pytest.raises(ValueError, match="must be a list of namespace names"):
        parse_control_plane_azure_spec(payload)


def test_parse_rejects_empty_allowed_namespace_entry() -> None:
    payload = _full_payload(allowedNamespaces=["default", ""])

    with pytest.raises(
        ValueError, match=r"allowedNamespaces\[1\] must be a non-empty string"
    ):
        parse_control_plane_azure_spec(payload)


def test_parse_allows_empty_allowed_namespaces_list() -> None:
    # Empty list is semantically "no namespace may reference this
    # identity" per the upstream CRD; we shouldn't *reject* it at parse
    # time because the contract has to round-trip cleanly, but we do
    # surface it as an empty list (not None) so callers can tell.
    payload = _full_payload(allowedNamespaces=[])

    parsed = parse_control_plane_azure_spec(payload)
    assert parsed.allowed_namespaces == []
