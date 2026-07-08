"""Config parsing for Kind management-cluster control-plane capabilities."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, Union
from uuid import UUID

from pydantic import Field, StrictBool, TypeAdapter, field_serializer

from lib.config import (
    EnabledConfig,
    NonEmptyStr,
    PulumiConfigModel,
    maybe_disabled,
)
from localenv import (
    AzureResourcePlacement,
    discover_azure_credentials,
    discover_azure_resource_placement,
)


CONTROL_PLANE_KIND_CHILD_CONFIG_KEY = "controlPlane"


class AllowedNamespacesConfig(PulumiConfigModel):
    list: tuple[NonEmptyStr, ...] | None = None
    selector: dict[str, object] | None = None


class AzureClusterIdentityBaseConfig(PulumiConfigModel):
    @field_serializer("type", check_fields=False)
    def serialize_type(self, identity_type: str) -> str:
        return identity_type

    @field_serializer("client_id", "tenant_id", check_fields=False)
    def serialize_uuid(self, value: UUID | None) -> str | None:
        return str(value) if value is not None else None

    client_id: UUID | None = None
    tenant_id: UUID | None = None
    allowed_namespaces: AllowedNamespacesConfig = Field(
        default_factory=AllowedNamespacesConfig
    )


class UserAssignedMSIClusterIdentityConfig(AzureClusterIdentityBaseConfig):
    type: Literal["UserAssignedMSI"] = "UserAssignedMSI"
    client_id: UUID | None = Field(
        default_factory=lambda: UUID(
            discover_azure_credentials(
                identity_types=("UserAssignedMSI",),
                raise_on_missing=True,
            )[0].client_id
        )
    )
    tenant_id: UUID | None = Field(
        default_factory=lambda data: UUID(
            discover_azure_credentials(
                identity_types=("UserAssignedMSI",),
                client_id=data["client_id"]
                if isinstance(data.get("client_id"), UUID)
                else None,
                raise_on_missing=True,
            )[0].tenant_id
        )
    )


class WorkloadIdentityClusterIdentityConfig(AzureClusterIdentityBaseConfig):
    type: Literal["WorkloadIdentity"] = "WorkloadIdentity"


AzureClusterIdentityConfig: TypeAlias = Annotated[
    Union[
        UserAssignedMSIClusterIdentityConfig,
        WorkloadIdentityClusterIdentityConfig,
    ],
    Field(discriminator="type"),
]


@maybe_disabled
class DockerInfrastructureProviderConfig(EnabledConfig):
    """Enabled Docker (CAPD) infrastructure provider settings."""


def _discover_default_resource_placement(
    data: dict[str, object],
) -> AzureResourcePlacement | None:
    if data.get("enabled") is not True:
        return None
    return discover_azure_resource_placement(raise_on_missing=True)


def _discover_default_subscription_id(data: dict[str, object]) -> UUID | None:
    placement = _discover_default_resource_placement(data)
    return UUID(placement.subscription_id) if placement is not None else None


def _discover_default_location(data: dict[str, object]) -> str | None:
    placement = _discover_default_resource_placement(data)
    return placement.location if placement is not None else None


def _discover_default_resource_group(data: dict[str, object]) -> str | None:
    placement = _discover_default_resource_placement(data)
    return placement.resource_group if placement is not None else None


def _discover_default_identity(
    data: dict[str, object],
) -> AzureClusterIdentityConfig | None:
    if data.get("enabled") is not True:
        return None
    credentials = discover_azure_credentials()
    if len(credentials) == 1:
        credential = credentials[0]
    elif not credentials:
        raise ValueError(
            "azure identity discovery found no usable credential matching "
            "configured identity"
        )
    else:
        raise ValueError(
            "azure identity discovery found multiple usable credentials; configure "
            "identity.type or identity.clientId"
        )
    return TypeAdapter(AzureClusterIdentityConfig).validate_python(
        {
            "type": credential.type,
            "clientId": credential.client_id,
            "tenantId": credential.tenant_id,
        }
    )


class AzureInfrastructureProviderConfig(PulumiConfigModel):
    """Enabled Azure (CAPZ) infrastructure provider settings and cluster identity."""

    @field_serializer("enabled")
    def serialize_enabled(self, enabled: bool) -> bool:
        return enabled

    enabled: StrictBool = False
    identity: AzureClusterIdentityConfig | None = Field(
        default_factory=_discover_default_identity
    )
    default_subscription_id: UUID | None = Field(
        default_factory=_discover_default_subscription_id
    )
    default_location: NonEmptyStr | None = Field(
        default_factory=_discover_default_location
    )
    default_resource_group: NonEmptyStr | None = Field(
        default_factory=_discover_default_resource_group
    )


class InfrastructureProvidersConfig(PulumiConfigModel):
    docker: DockerInfrastructureProviderConfig | None = None
    azure: AzureInfrastructureProviderConfig | None = None


@maybe_disabled
class ControlPlaneAWXConfig(EnabledConfig):
    """Enabled AWX deployment settings."""


class ControlPlaneDeploymentsConfig(PulumiConfigModel):
    awx: ControlPlaneAWXConfig = ControlPlaneAWXConfig(enabled=False)


class ControlPlaneKindConfig(PulumiConfigModel):
    infrastructure_providers: InfrastructureProvidersConfig = InfrastructureProvidersConfig()
    deployments: ControlPlaneDeploymentsConfig = ControlPlaneDeploymentsConfig()
