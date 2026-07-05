"""Config parsing for Kind management-cluster control-plane capabilities."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias, Union, cast
from uuid import UUID

from pydantic import Field, StrictBool, field_serializer

from localenv import AzureEnvironment
from lib.config import (
    DisabledConfig,
    EnabledConfig,
    NonEmptyStr,
    PulumiConfigModel,
    maybe_disabled,
)


CONTROL_PLANE_KIND_CHILD_CONFIG_KEY = "controlPlane"


def _single_discovered_client_id(azure_environment: AzureEnvironment) -> UUID:
    if len(azure_environment.client_ids) != 1:
        raise ValueError(
            "azure infrastructure provider discovery requires exactly one "
            "managed identity clientId when clientId is not explicitly configured"
        )
    return UUID(azure_environment.client_ids[0])


class AzureClusterIdentityBaseConfig(PulumiConfigModel):
    @field_serializer("type", check_fields=False)
    def serialize_type(self, identity_type: str) -> str:
        return identity_type

    client_id: UUID
    tenant_id: UUID
    allowed_namespaces: list[NonEmptyStr] | None = None


class UserAssignedMSIClusterIdentityConfig(AzureClusterIdentityBaseConfig):
    type: Literal["UserAssignedMSI"] = "UserAssignedMSI"


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


@maybe_disabled
class AzureInfrastructureProviderConfig(EnabledConfig):
    """Enabled Azure (CAPZ) infrastructure provider settings and cluster identity."""

    client_id: UUID | None = Field(default=None, exclude=True)
    default_subscription_id: UUID | None = None
    identity: AzureClusterIdentityConfig | None = None
    skip_in_cluster_preflight: StrictBool = False


class InfrastructureProvidersConfig(PulumiConfigModel):
    docker: DockerInfrastructureProviderConfig | None = None
    azure: AzureInfrastructureProviderConfig | None = None

    def apply_local_environment_discovery(
        self,
        *,
        infrastructure_providers: tuple[str, ...],
        azure_environment: AzureEnvironment | None = None,
    ) -> InfrastructureProvidersConfig:
        docker = self.docker
        if docker is None:
            docker = DockerInfrastructureProviderConfig(
                enabled="docker" in infrastructure_providers
            )

        azure = self.azure
        azure_config = (
            cast(Any, azure)
            if isinstance(azure, AzureInfrastructureProviderConfig)
            else None
        )
        if azure_config is not None and (
            azure_config.default_subscription_id is None or azure_config.identity is None
        ):
            if azure_environment is None:
                raise ValueError(
                    "partial azure infrastructure provider config requires "
                    "discovered Azure environment"
                )
            azure = AzureInfrastructureProviderConfig(
                enabled=True,
                default_subscription_id=UUID(azure_environment.host_subscription_id),
                identity=UserAssignedMSIClusterIdentityConfig(
                    client_id=(
                        azure_config.client_id
                        if azure_config.client_id is not None
                        else _single_discovered_client_id(azure_environment)
                    ),
                    tenant_id=UUID(azure_environment.tenant_id),
                    allowed_namespaces=[],
                ),
                skip_in_cluster_preflight=azure_config.skip_in_cluster_preflight,
            )

        if (
            azure is None
            and azure_environment is not None
            and "azure" in infrastructure_providers
        ):
            azure = AzureInfrastructureProviderConfig(
                enabled=True,
                default_subscription_id=UUID(azure_environment.host_subscription_id),
                identity=UserAssignedMSIClusterIdentityConfig(
                    client_id=_single_discovered_client_id(azure_environment),
                    tenant_id=UUID(azure_environment.tenant_id),
                    allowed_namespaces=[],
                ),
            )

        return InfrastructureProvidersConfig(
            docker=docker,
            azure=azure,
        )


@maybe_disabled
class ControlPlaneAWXConfig(EnabledConfig):
    """Enabled AWX deployment settings."""


class ControlPlaneDeploymentsConfig(PulumiConfigModel):
    awx: ControlPlaneAWXConfig = ControlPlaneAWXConfig(enabled=True)


class ControlPlaneKindConfig(PulumiConfigModel):
    infrastructure_providers: InfrastructureProvidersConfig = InfrastructureProvidersConfig()
    deployments: ControlPlaneDeploymentsConfig = ControlPlaneDeploymentsConfig()

