"""Config parsing for Kind management-cluster control-plane capabilities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


CONTROL_PLANE_KIND_CHILD_CONFIG_KEY = "controlPlane"
LEGACY_LOCAL_CONTROL_PLANE_TYPE = "ca4s:control_plane:ControlPlaneLocal"
LEGACY_LOCAL_AWX_CONTROL_PLANE_TYPE = "ca4s:control_plane:LocalAWXControlPlane"
LEGACY_AZURE_CONTROL_PLANE_TYPE = "ca4s:control_plane:ControlPlaneAzure"

_CONFIG_AZURE = "azure"
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
class ControlPlaneAWXConfig:
    enable_awx: bool = True


@dataclass(frozen=True)
class ControlPlaneAzureConfig:
    client_id: str
    principal_id: str
    tenant_id: str
    subscription_id: str
    allowed_namespaces: list[str] | None = None
    infrastructure_providers: tuple[str, ...] = ("azure",)
    skip_in_cluster_preflight: bool = False


@dataclass(frozen=True)
class ControlPlaneKindConfig:
    infrastructure_providers: tuple[str, ...] = ("docker",)
    enable_awx: bool = True
    azure: ControlPlaneAzureConfig | None = None


def _require_mapping(field_path: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object")
    return value


def _parse_control_plane_awx_config(value: object | None) -> ControlPlaneAWXConfig:
    if value is None:
        return ControlPlaneAWXConfig()
    if isinstance(value, ControlPlaneAWXConfig):
        return value

    spec = _require_mapping(CONTROL_PLANE_KIND_CHILD_CONFIG_KEY, value)
    awx_value = spec.get(_CONFIG_AWX)
    if awx_value is None:
        return ControlPlaneAWXConfig()

    awx = _require_mapping(
        f"{CONTROL_PLANE_KIND_CHILD_CONFIG_KEY}.{_CONFIG_AWX}",
        awx_value,
    )
    enabled = awx.get(_CONFIG_ENABLED, True)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"{CONTROL_PLANE_KIND_CHILD_CONFIG_KEY}.{_CONFIG_AWX}.{_CONFIG_ENABLED} "
            "must be a boolean"
        )
    return ControlPlaneAWXConfig(enable_awx=enabled)


def parse_control_plane_kind_config(value: object | None) -> ControlPlaneKindConfig:
    if value is None:
        return ControlPlaneKindConfig()
    if isinstance(value, ControlPlaneKindConfig):
        return value

    spec = _require_mapping(CONTROL_PLANE_KIND_CHILD_CONFIG_KEY, value)
    awx_config = _parse_control_plane_awx_config(spec)
    azure_value = spec.get(_CONFIG_AZURE)
    azure = (
        _parse_control_plane_azure_config(azure_value)
        if azure_value is not None
        else None
    )
    infrastructure_providers = (
        azure.infrastructure_providers if azure is not None else ("docker",)
    )
    return ControlPlaneKindConfig(
        infrastructure_providers=infrastructure_providers,
        enable_awx=awx_config.enable_awx,
        azure=azure,
    )


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


def _parse_control_plane_azure_config(
    value: object | None,
    *,
    field_path: str = "controlPlane.azure",
) -> ControlPlaneAzureConfig:
    if value is None:
        raise ValueError(
            "missing required Azure control-plane config under "
            f"{field_path!r}; the outer "
            "stack_azure.py must pass PKOBootstrap(config={...}) with an "
            f"{field_path!r} entry"
        )
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{field_path!r} config must be an "
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
            f"{field_path}.{config_key}",
            value.get(config_key),
        )

    allowed_namespaces = _parse_allowed_namespaces(
        f"{field_path}.{_CONFIG_ALLOWED_NAMESPACES}",
        value.get(_CONFIG_ALLOWED_NAMESPACES),
    )
    infrastructure_providers = _parse_infrastructure_providers(
        f"{field_path}.{_CONFIG_INFRASTRUCTURE_PROVIDERS}",
        value.get(_CONFIG_INFRASTRUCTURE_PROVIDERS),
    )

    skip_field = value.get(_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT)
    if skip_field is not None and not isinstance(skip_field, bool):
        raise ValueError(
            f"{field_path}.{_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT} must be a boolean; "
            f"got {type(skip_field).__name__}"
        )

    return ControlPlaneAzureConfig(
        **fields,
        allowed_namespaces=allowed_namespaces,
        infrastructure_providers=infrastructure_providers,
        skip_in_cluster_preflight=bool(skip_field),
    )


def _build_control_plane_azure_payload(
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
    return child


def build_control_plane_kind_child_config(
    *,
    enable_awx: bool = True,
    azure: dict[str, object] | None = None,
) -> dict[str, object]:
    control_plane: dict[str, object] = {}
    if not enable_awx:
        control_plane[_CONFIG_AWX] = {_CONFIG_ENABLED: False}
    if azure is not None:
        control_plane[_CONFIG_AZURE] = azure
    return {CONTROL_PLANE_KIND_CHILD_CONFIG_KEY: control_plane}


def build_control_plane_kind_azure_child_config(
    *,
    client_id: str,
    principal_id: str,
    tenant_id: str,
    subscription_id: str,
    allowed_namespaces: list[str] | None = None,
    infrastructure_providers: tuple[str, ...] = ("azure",),
    skip_in_cluster_preflight: bool = False,
    enable_awx: bool = False,
) -> dict[str, object]:
    return build_control_plane_kind_child_config(
        enable_awx=enable_awx,
        azure=_build_control_plane_azure_payload(
            client_id=client_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            allowed_namespaces=allowed_namespaces,
            infrastructure_providers=infrastructure_providers,
            skip_in_cluster_preflight=skip_in_cluster_preflight,
        ),
    )
