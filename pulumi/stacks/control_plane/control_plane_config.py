"""Config parsing for Kind management-cluster control-plane capabilities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY = "controlPlane"
CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY = "azure"
LEGACY_LOCAL_CONTROL_PLANE_TYPE = "ca4s:control_plane:ControlPlaneLocal"
LEGACY_LOCAL_AWX_CONTROL_PLANE_TYPE = "ca4s:control_plane:LocalAWXControlPlane"
LEGACY_AZURE_CONTROL_PLANE_TYPE = "ca4s:control_plane:ControlPlaneAzure"

_CONFIG_AWX = "awx"
_CONFIG_ENABLED = "enabled"
_CONFIG_CLIENT_ID = "clientId"
_CONFIG_PRINCIPAL_ID = "principalId"
_CONFIG_TENANT_ID = "tenantId"
_CONFIG_SUBSCRIPTION_ID = "subscriptionId"
_CONFIG_ALLOWED_NAMESPACES = "allowedNamespaces"
_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT = "skipInClusterPreflight"
_CONFIG_INFRASTRUCTURE_PROVIDERS = "infrastructureProviders"
_GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class ControlPlaneLocalSpec:
    enable_awx: bool = True


@dataclass(frozen=True)
class ControlPlaneAzureSpec:
    client_id: str
    principal_id: str
    tenant_id: str
    subscription_id: str
    allowed_namespaces: list[str] | None = None
    infrastructure_providers: tuple[str, ...] = ("azure",)
    skip_in_cluster_preflight: bool = False


def _require_mapping(field_path: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object")
    return value


def parse_control_plane_local_spec(value: object | None) -> ControlPlaneLocalSpec:
    if value is None:
        return ControlPlaneLocalSpec()
    if isinstance(value, ControlPlaneLocalSpec):
        return value

    spec = _require_mapping(CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY, value)
    awx_value = spec.get(_CONFIG_AWX)
    if awx_value is None:
        return ControlPlaneLocalSpec()

    awx = _require_mapping(
        f"{CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY}.{_CONFIG_AWX}",
        awx_value,
    )
    enabled = awx.get(_CONFIG_ENABLED, True)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"{CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY}.{_CONFIG_AWX}.{_CONFIG_ENABLED} "
            "must be a boolean"
        )
    return ControlPlaneLocalSpec(enable_awx=enabled)


def _require_guid(field_path: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_path} must be a non-empty string")
    if not _GUID_PATTERN.match(value):
        raise ValueError(
            f"{field_path} must be a GUID in 8-4-4-4-12 hex layout; got {value!r}"
        )
    return value


def _parse_allowed_namespaces(
    field_path: str, value: object | None
) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_path} must be a list of namespace names")
    parsed: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                f"{field_path}[{index}] must be a non-empty string"
            )
        parsed.append(entry)
    return parsed


def _parse_infrastructure_providers(
    field_path: str, value: object | None
) -> tuple[str, ...]:
    if value is None:
        return ("azure",)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_path} must be a list of provider names")
    parsed: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                f"{field_path}[{index}] must be a non-empty string"
            )
        parsed.append(entry)
    if "azure" not in parsed:
        raise ValueError(f"{field_path} must include 'azure'")
    return tuple(parsed)


def parse_control_plane_azure_spec(value: object | None) -> ControlPlaneAzureSpec:
    if value is None:
        raise ValueError(
            "missing required Azure control-plane config under "
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r}; the outer "
            "stack_azure.py must pass PKOBootstrap(config={...}) with an "
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r} entry"
        )
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r} config must be an "
            f"object; got {type(value).__name__}"
        )

    fields: dict[str, str] = {}
    for config_key, field_name in (
        (_CONFIG_CLIENT_ID, "client_id"),
        (_CONFIG_PRINCIPAL_ID, "principal_id"),
        (_CONFIG_TENANT_ID, "tenant_id"),
        (_CONFIG_SUBSCRIPTION_ID, "subscription_id"),
    ):
        fields[field_name] = _require_guid(
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}.{config_key}",
            value.get(config_key),
        )

    allowed_namespaces = _parse_allowed_namespaces(
        f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}.{_CONFIG_ALLOWED_NAMESPACES}",
        value.get(_CONFIG_ALLOWED_NAMESPACES),
    )
    infrastructure_providers = _parse_infrastructure_providers(
        f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}.{_CONFIG_INFRASTRUCTURE_PROVIDERS}",
        value.get(_CONFIG_INFRASTRUCTURE_PROVIDERS),
    )

    skip_field = value.get(_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT)
    if skip_field is not None and not isinstance(skip_field, bool):
        raise ValueError(
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}."
            f"{_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT} must be a boolean; "
            f"got {type(skip_field).__name__}"
        )

    return ControlPlaneAzureSpec(
        **fields,
        allowed_namespaces=allowed_namespaces,
        infrastructure_providers=infrastructure_providers,
        skip_in_cluster_preflight=bool(skip_field),
    )


def build_control_plane_azure_child_config(
    *,
    client_id: str,
    principal_id: str,
    tenant_id: str,
    subscription_id: str,
    allowed_namespaces: list[str] | None = None,
    infrastructure_providers: tuple[str, ...] = ("azure",),
    skip_in_cluster_preflight: bool = False,
) -> dict[str, object]:
    child: dict[str, object] = {
        _CONFIG_CLIENT_ID: client_id,
        _CONFIG_PRINCIPAL_ID: principal_id,
        _CONFIG_TENANT_ID: tenant_id,
        _CONFIG_SUBSCRIPTION_ID: subscription_id,
    }
    if allowed_namespaces is not None:
        child[_CONFIG_ALLOWED_NAMESPACES] = list(allowed_namespaces)
    if infrastructure_providers != ("azure",):
        child[_CONFIG_INFRASTRUCTURE_PROVIDERS] = list(infrastructure_providers)
    if skip_in_cluster_preflight:
        child[_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT] = True
    return {CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY: child}
