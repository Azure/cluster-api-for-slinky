# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Unit tests for AzureClusterIdentity manifest normalization."""

from __future__ import annotations

from stacks.control_plane.azure._cluster_identity import (
    _allowed_namespaces_spec,
    _identity_spec,
)
from stacks.control_plane.control_plane_config import (
    AllowedNamespacesConfig,
    UserAssignedMSIClusterIdentityConfig,
)


_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"


def test_allowed_namespaces_spec_defaults_to_capz_allow_all_object() -> None:
    assert _allowed_namespaces_spec(None) == {}
    assert _allowed_namespaces_spec(AllowedNamespacesConfig()) == {}


def test_allowed_namespaces_spec_serializes_config_model() -> None:
    assert _allowed_namespaces_spec(
        AllowedNamespacesConfig(list=("default", "tenant-a"))
    ) == {"list": ["default", "tenant-a"]}
    assert _allowed_namespaces_spec(
        AllowedNamespacesConfig(selector={"matchLabels": {"team": "slinky"}})
    ) == {"selector": {"matchLabels": {"team": "slinky"}}}


def test_allowed_namespaces_spec_preserves_empty_list_semantics() -> None:
    assert _allowed_namespaces_spec(AllowedNamespacesConfig(list=())) == {"list": []}
    assert _allowed_namespaces_spec({"list": []}) == {"list": []}


def test_allowed_namespaces_spec_accepts_rendered_mapping() -> None:
    assert _allowed_namespaces_spec(
        {"list": ("default",), "selector": None}
    ) == {"list": ("default",)}


def test_identity_spec_renders_capz_identity_shape() -> None:
    assert _identity_spec(
        UserAssignedMSIClusterIdentityConfig(
            client_id=_CLIENT_ID,
            tenant_id=_TENANT_ID,
            allowed_namespaces=AllowedNamespacesConfig(list=("tenant-a",)),
        )
    ) == {
        "type": "UserAssignedMSI",
        "tenantID": _TENANT_ID,
        "clientID": _CLIENT_ID,
        "allowedNamespaces": {"list": ["tenant-a"]},
    }