"""Unit tests for Azure control-plane config parsing and rendering."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import TypeAdapter

from localenv import (
    AzureDiscoveredCredential,
    AzureEnvironment,
    AzureIdentityType,
    AzureResourcePlacement,
)
import stacks.control_plane.control_plane_config as control_plane_config_module
from stacks.control_plane.control_plane_config import (
    CONTROL_PLANE_KIND_CHILD_CONFIG_KEY,
    AllowedNamespacesConfig,
    AzureInfrastructureProviderConfig,
    AzureClusterIdentityConfig,
    ControlPlaneAWXConfig,
    ControlPlaneDeploymentsConfig,
    DockerInfrastructureProviderConfig,
    InfrastructureProvidersConfig,
    ControlPlaneKindConfig,
    UserAssignedMSIClusterIdentityConfig,
    WorkloadIdentityClusterIdentityConfig,
)


# Real-looking GUIDs (8-4-4-4-12 hex); arbitrary values, not real
# identities. Reused across tests so each one focuses on what it's
# actually checking.
_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"
_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"
_OTHER_SUBSCRIPTION_ID = "55555555-5555-5555-5555-555555555555"
_WORKLOAD_CLIENT_ID = "66666666-6666-6666-6666-666666666666"
_LOCATION = "westus2"
_RESOURCE_GROUP = "host-rg"
_DEFAULT_ALLOWED_NAMESPACES = object()


def _azure_environment() -> AzureEnvironment:
    return AzureEnvironment(
        credentials=(
            AzureDiscoveredCredential(
                type="UserAssignedMSI",
                client_id=_CLIENT_ID,
                tenant_id=_TENANT_ID,
            ),
        ),
        host_subscription_id=_SUBSCRIPTION_ID,
        host_location=_LOCATION,
        host_resource_group=_RESOURCE_GROUP,
    )


def _azure_environment_with_workload_identity() -> AzureEnvironment:
    return AzureEnvironment(
        credentials=(
            AzureDiscoveredCredential(
                type="UserAssignedMSI",
                client_id=_CLIENT_ID,
                tenant_id=_TENANT_ID,
            ),
            AzureDiscoveredCredential(
                type="WorkloadIdentity",
                client_id=_WORKLOAD_CLIENT_ID,
                tenant_id=_TENANT_ID,
            ),
        ),
        host_subscription_id=_SUBSCRIPTION_ID,
        host_location=_LOCATION,
        host_resource_group=_RESOURCE_GROUP,
    )


def _mock_azure_discovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    azure_environment: AzureEnvironment | None = None,
) -> None:
    environment = azure_environment or _azure_environment()

    def _discover_credentials(
        *,
        identity_types: tuple[AzureIdentityType, ...] = (
            "UserAssignedMSI",
            "WorkloadIdentity",
        ),
        client_id: UUID | None = None,
        raise_on_missing: bool = False,
    ) -> tuple[AzureDiscoveredCredential, ...]:
        credentials = tuple(
            credential
            for credential in environment.credentials
            if credential.type in identity_types
            and (client_id is None or credential.client_id == str(client_id))
        )
        if not credentials and raise_on_missing:
            raise ValueError(
                "azure identity discovery found no usable credential matching "
                "configured identity"
            )
        return credentials

    def _discover_resource_placement(
        *,
        raise_on_missing: bool = False,
    ) -> AzureResourcePlacement:
        return AzureResourcePlacement(
            subscription_id=environment.host_subscription_id,
            location=environment.host_location,
            resource_group=environment.host_resource_group,
        )

    monkeypatch.setattr(
        control_plane_config_module,
        "discover_azure_credentials",
        _discover_credentials,
    )
    monkeypatch.setattr(
        control_plane_config_module,
        "discover_azure_resource_placement",
        _discover_resource_placement,
    )


def _full_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "UserAssignedMSI",
        "clientId": _CLIENT_ID,
        "tenantId": _TENANT_ID,
    }
    payload.update(overrides)
    return payload


def _parse_identity(payload: object) -> AzureClusterIdentityConfig:
    return TypeAdapter(AzureClusterIdentityConfig).validate_python(payload)


def _parse_azure_provider(payload: object):
    parsed = ControlPlaneKindConfig.model_validate(
        {
            "infrastructureProviders": {
                "docker": {"enabled": False},
                "azure": payload,
            }
        }
    )
    return parsed.infrastructure_providers.azure


def _azure_provider(config):
    azure = config.infrastructure_providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    return azure


def _assert_user_assigned_identity(
    identity: object,
    *,
    allowed_namespaces: AllowedNamespacesConfig | object = _DEFAULT_ALLOWED_NAMESPACES,
) -> None:
    assert isinstance(identity, UserAssignedMSIClusterIdentityConfig)
    assert str(identity.client_id) == _CLIENT_ID
    assert str(identity.tenant_id) == _TENANT_ID
    if allowed_namespaces is _DEFAULT_ALLOWED_NAMESPACES:
        allowed_namespaces = AllowedNamespacesConfig()
    assert identity.allowed_namespaces == allowed_namespaces


def _assert_workload_identity(
    identity: object,
    *,
    client_id: str | None = _WORKLOAD_CLIENT_ID,
    tenant_id: str | None = _TENANT_ID,
    allowed_namespaces: AllowedNamespacesConfig | object = _DEFAULT_ALLOWED_NAMESPACES,
) -> None:
    assert isinstance(identity, WorkloadIdentityClusterIdentityConfig)
    assert (str(identity.client_id) if identity.client_id is not None else None) == client_id
    assert (str(identity.tenant_id) if identity.tenant_id is not None else None) == tenant_id
    if allowed_namespaces is _DEFAULT_ALLOWED_NAMESPACES:
        allowed_namespaces = AllowedNamespacesConfig()
    assert identity.allowed_namespaces == allowed_namespaces



def _control_plane_child_config(config: ControlPlaneKindConfig) -> dict[str, object]:
    return {CONTROL_PLANE_KIND_CHILD_CONFIG_KEY: config.to_config()}


def test_build_and_parse_control_plane_kind_round_trip_minimum_fields() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    default_location=_LOCATION,
                    default_resource_group=_RESOURCE_GROUP,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                    ),
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    assert built == {
        CONTROL_PLANE_KIND_CHILD_CONFIG_KEY: {
            "infrastructureProviders": {
                "docker": {"enabled": False},
                "azure": {
                    "enabled": True,
                    "defaultSubscriptionId": _SUBSCRIPTION_ID,
                    "defaultLocation": _LOCATION,
                    "defaultResourceGroup": _RESOURCE_GROUP,
                    "identity": {
                        "type": "UserAssignedMSI",
                        "clientId": _CLIENT_ID,
                        "tenantId": _TENANT_ID,
                    },
                },
            },
        },
    }

    parsed = ControlPlaneKindConfig.model_validate(
        built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    )
    assert parsed.deployments.awx.enabled is False
    azure = _azure_provider(parsed)
    assert isinstance(azure.identity, UserAssignedMSIClusterIdentityConfig)
    assert str(azure.identity.client_id) == _CLIENT_ID
    assert str(azure.identity.tenant_id) == _TENANT_ID
    assert azure.identity.type == "UserAssignedMSI"
    assert azure.default_subscription_id is not None
    assert str(azure.default_subscription_id) == _SUBSCRIPTION_ID
    assert azure.default_location == _LOCATION
    assert azure.default_resource_group == _RESOURCE_GROUP


def test_builds_enabled_azure_provider_from_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_discovery(monkeypatch)

    providers = InfrastructureProvidersConfig(
        docker=DockerInfrastructureProviderConfig(enabled=True),
        azure=AzureInfrastructureProviderConfig(enabled=True),
    )

    assert isinstance(providers.docker, DockerInfrastructureProviderConfig)
    azure = providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    assert str(azure.default_subscription_id) == _SUBSCRIPTION_ID
    assert azure.default_location == _LOCATION
    assert azure.default_resource_group == _RESOURCE_GROUP
    _assert_user_assigned_identity(azure.identity)


def test_provider_config_preserves_explicit_azure_provider() -> None:
    providers = InfrastructureProvidersConfig(
        docker=DockerInfrastructureProviderConfig(enabled=True),
        azure=AzureInfrastructureProviderConfig(
            enabled=True,
            default_subscription_id=_SUBSCRIPTION_ID,
            default_location=_LOCATION,
            default_resource_group=_RESOURCE_GROUP,
            identity=UserAssignedMSIClusterIdentityConfig(
                client_id=_CLIENT_ID,
                tenant_id=_TENANT_ID,
            ),
        ),
    )

    assert isinstance(providers.docker, DockerInfrastructureProviderConfig)
    assert isinstance(providers.azure, AzureInfrastructureProviderConfig)


def test_provider_config_leaves_azure_unspecified() -> None:
    providers = InfrastructureProvidersConfig(
        docker=DockerInfrastructureProviderConfig(enabled=True),
    )

    assert isinstance(providers.docker, DockerInfrastructureProviderConfig)
    assert providers.azure is None


def test_provider_config_preserves_explicit_disabled_azure() -> None:
    providers = InfrastructureProvidersConfig(
        docker=DockerInfrastructureProviderConfig(enabled=True),
        azure=AzureInfrastructureProviderConfig(enabled=False),
    )

    assert isinstance(providers.azure, AzureInfrastructureProviderConfig)
    assert providers.azure.enabled is False


def test_build_omits_default_allowed_namespaces_object() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    default_location=_LOCATION,
                    default_resource_group=_RESOURCE_GROUP,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                    ),
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    # The config carrier omits default empty objects. Parsing rehydrates
    # the empty CAPZ allowedNamespaces object, which renders as allow-all
    # on the CR side.
    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    identity = control_plane["infrastructureProviders"]["azure"]["identity"]
    assert isinstance(identity, dict)
    assert "allowedNamespaces" not in identity

    parsed = ControlPlaneKindConfig.model_validate(control_plane)
    azure = _azure_provider(parsed)
    assert azure.identity is not None
    assert azure.identity.allowed_namespaces == AllowedNamespacesConfig()


def test_build_serializes_allowed_namespaces_list() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    default_location=_LOCATION,
                    default_resource_group=_RESOURCE_GROUP,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                        allowed_namespaces=AllowedNamespacesConfig(
                            list=("default", "tenant-a")
                        ),
                    ),
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    identity = control_plane["infrastructureProviders"]["azure"]["identity"]
    assert identity == {
        "type": "UserAssignedMSI",
        "clientId": _CLIENT_ID,
        "tenantId": _TENANT_ID,
        "allowedNamespaces": {"list": ["default", "tenant-a"]},
    }

    parsed = ControlPlaneKindConfig.model_validate(control_plane)
    azure = _azure_provider(parsed)
    assert azure.identity is not None
    assert azure.identity.allowed_namespaces == AllowedNamespacesConfig(
        list=("default", "tenant-a")
    )


def test_build_serializes_allowed_namespaces_selector() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    default_location=_LOCATION,
                    default_resource_group=_RESOURCE_GROUP,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                        allowed_namespaces=AllowedNamespacesConfig(
                            selector={"matchLabels": {"team": "slinky"}}
                        ),
                    ),
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    identity = control_plane["infrastructureProviders"]["azure"]["identity"]
    assert identity["allowedNamespaces"] == {
        "selector": {"matchLabels": {"team": "slinky"}}
    }


def test_parse_kind_config_without_azure_leaves_config_none() -> None:
    parsed = ControlPlaneKindConfig()
    assert not isinstance(
        parsed.infrastructure_providers.azure,
        AzureInfrastructureProviderConfig,
    )


def test_parse_rejects_non_mapping_payload() -> None:
    with pytest.raises(ValueError, match="Input should be a valid dictionary"):
        _parse_identity("not-a-dict")


def test_parse_accepts_explicit_user_assigned_msi_type() -> None:
    parsed = _parse_identity(
        _full_payload(type="UserAssignedMSI")
    )

    assert parsed.type == "UserAssignedMSI"


def test_parse_accepts_workload_identity_type() -> None:
    parsed = _parse_identity(
        _full_payload(type="WorkloadIdentity")
    )

    assert parsed.type == "WorkloadIdentity"


def test_parse_rejects_unsupported_identity_type() -> None:
    with pytest.raises(ValueError, match="type"):
        _parse_identity(_full_payload(type="ServicePrincipal"))


def test_parse_rejects_missing_identity_type() -> None:
    payload = _full_payload()
    del payload["type"]

    with pytest.raises(ValueError, match="type"):
        _parse_identity(payload)


def test_parse_hydrates_missing_user_assigned_msi_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_discovery(monkeypatch)
    payload = _full_payload()
    del payload["clientId"]

    parsed = _parse_identity(payload)

    _assert_user_assigned_identity(parsed)


def test_parse_hydrates_missing_user_assigned_msi_tenant_id_with_client_id_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered_client_ids: list[UUID | None] = []

    def _discover_credentials(
        *,
        identity_types: tuple[AzureIdentityType, ...] = (
            "UserAssignedMSI",
            "WorkloadIdentity",
        ),
        client_id: UUID | None = None,
        raise_on_missing: bool = False,
    ) -> tuple[AzureDiscoveredCredential, ...]:
        discovered_client_ids.append(client_id)
        credentials = tuple(
            credential
            for credential in _azure_environment().credentials
            if credential.type in identity_types
            and (client_id is None or credential.client_id == str(client_id))
        )
        if not credentials and raise_on_missing:
            raise ValueError(
                "azure identity discovery found no usable credential matching "
                "configured identity"
            )
        return credentials

    monkeypatch.setattr(
        control_plane_config_module,
        "discover_azure_credentials",
        _discover_credentials,
    )
    payload = _full_payload()
    del payload["tenantId"]

    parsed = _parse_identity(payload)

    _assert_user_assigned_identity(parsed)
    assert discovered_client_ids == [UUID(_CLIENT_ID)]


@pytest.mark.parametrize(
    "bad_guid_field",
    ["clientId", "tenantId"],
)
def test_parse_rejects_malformed_guid(bad_guid_field: str) -> None:
    payload = _full_payload(**{bad_guid_field: "not-a-guid"})

    with pytest.raises(
        ValueError,
        match=rf"(?s){bad_guid_field}.*Input should be a valid UUID",
    ):
        _parse_identity(payload)


def test_parse_rejects_non_string_guid() -> None:
    # Numbers can sneak in if config is hand-edited; the GUID check
    # must reject them before reaching the regex.
    payload = _full_payload(clientId=12345)

    with pytest.raises(ValueError, match="clientId"):
        _parse_identity(payload)


def test_parse_rejects_non_list_allowed_namespaces() -> None:
    payload = _full_payload(allowedNamespaces="default")

    with pytest.raises(ValueError, match="allowedNamespaces"):
        _parse_identity(payload)


def test_parse_rejects_empty_allowed_namespace_entry() -> None:
    payload = _full_payload(allowedNamespaces={"list": ["default", ""]})

    with pytest.raises(ValueError, match="allowedNamespaces"):
        _parse_identity(payload)


def test_parse_allows_empty_allowed_namespaces_list() -> None:
    # Empty list is semantically "no namespace may reference this
    # identity" per the upstream CRD; we shouldn't *reject* it at parse
    # time because the contract has to round-trip cleanly, but we do
    # surface it as an empty list (not None) so callers can tell.
    payload = _full_payload(allowedNamespaces={"list": []})

    parsed = _parse_identity(payload)
    assert parsed.allowed_namespaces == AllowedNamespacesConfig(list=())


def test_provider_config_completes_partial_azure_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_discovery(monkeypatch)

    providers = InfrastructureProvidersConfig.model_validate(
        {
            "azure": {
                "enabled": True,
                "identity": {
                    "type": "UserAssignedMSI",
                    "clientId": _CLIENT_ID,
                },
            }
        }
    )

    azure = providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    assert str(azure.default_subscription_id) == _SUBSCRIPTION_ID
    assert azure.default_location == _LOCATION
    assert azure.default_resource_group == _RESOURCE_GROUP
    _assert_user_assigned_identity(azure.identity)


def test_azure_provider_discovery_preserves_explicit_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_discovery(monkeypatch)

    azure = AzureInfrastructureProviderConfig(
        enabled=True,
        default_subscription_id=_OTHER_SUBSCRIPTION_ID,
    )

    assert str(azure.default_subscription_id) == _OTHER_SUBSCRIPTION_ID
    assert azure.default_location == _LOCATION
    assert azure.default_resource_group == _RESOURCE_GROUP
    _assert_user_assigned_identity(azure.identity)


def test_provider_config_uses_discovered_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_discovery(monkeypatch)

    providers = InfrastructureProvidersConfig(
        azure=AzureInfrastructureProviderConfig(enabled=True),
    )

    azure = providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    _assert_user_assigned_identity(azure.identity)


def test_provider_config_rejects_ambiguous_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_discovery(
        monkeypatch,
        azure_environment=_azure_environment_with_workload_identity(),
    )

    with pytest.raises(ValueError, match="multiple usable credentials"):
        InfrastructureProvidersConfig(
            azure=AzureInfrastructureProviderConfig(enabled=True),
        )


def test_provider_config_keeps_configured_workload_identity_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_credential_discovery(
        *,
        identity_types: tuple[AzureIdentityType, ...] = (
            "UserAssignedMSI",
            "WorkloadIdentity",
        ),
        client_id: UUID | None = None,
        raise_on_missing: bool = False,
    ) -> tuple[AzureDiscoveredCredential, ...]:
        raise AssertionError("workload identity must not hydrate from outer Pulumi")

    monkeypatch.setattr(
        control_plane_config_module,
        "discover_azure_credentials",
        _fail_credential_discovery,
    )

    providers = InfrastructureProvidersConfig.model_validate(
        {
            "azure": {
                "enabled": True,
                "defaultSubscriptionId": _SUBSCRIPTION_ID,
                "defaultLocation": _LOCATION,
                "defaultResourceGroup": _RESOURCE_GROUP,
                "identity": {"type": "WorkloadIdentity"},
            }
        }
    )

    azure = providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    _assert_workload_identity(
        azure.identity,
        client_id=None,
        tenant_id=None,
    )


def test_apply_local_environment_discovery_requires_discovery_for_partial_azure_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _discover_credentials(
        *,
        identity_types: tuple[AzureIdentityType, ...] = (
            "UserAssignedMSI",
            "WorkloadIdentity",
        ),
        client_id: UUID | None = None,
        raise_on_missing: bool = False,
    ) -> tuple[AzureDiscoveredCredential, ...]:
        credentials = tuple(
            credential
            for credential in _azure_environment().credentials
            if credential.type in identity_types
            and (client_id is None or credential.client_id == str(client_id))
        )
        if not credentials and raise_on_missing:
            raise ValueError(
                "azure identity discovery found no usable credential matching "
                "configured identity"
            )
        return credentials

    def _missing_resource_placement(
        *,
        raise_on_missing: bool = False,
    ) -> None:
        if raise_on_missing:
            raise ValueError(
                "azure resource placement discovery found no usable host environment"
            )
        return None

    monkeypatch.setattr(
        control_plane_config_module,
        "discover_azure_credentials",
        _discover_credentials,
    )
    monkeypatch.setattr(
        control_plane_config_module,
        "discover_azure_resource_placement",
        _missing_resource_placement,
    )

    with pytest.raises(ValueError, match="azure resource placement discovery"):
        InfrastructureProvidersConfig.model_validate(
            {
                "azure": {
                    "enabled": True,
                    "identity": {
                        "type": "UserAssignedMSI",
                        "clientId": _CLIENT_ID,
                    },
                }
            }
        )


def test_workload_identity_round_trips() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    default_location=_LOCATION,
                    default_resource_group=_RESOURCE_GROUP,
                    identity=WorkloadIdentityClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                    ),
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    assert control_plane["infrastructureProviders"]["azure"] == {
        "enabled": True,
        "defaultSubscriptionId": _SUBSCRIPTION_ID,
        "defaultLocation": _LOCATION,
        "defaultResourceGroup": _RESOURCE_GROUP,
        "identity": {
            "type": "WorkloadIdentity",
            "clientId": _CLIENT_ID,
            "tenantId": _TENANT_ID,
        },
    }

    parsed = ControlPlaneKindConfig.model_validate(control_plane)
    azure = _azure_provider(parsed)
    assert azure.identity is not None
    assert azure.identity.type == "WorkloadIdentity"


def test_parse_rejects_removed_skip_in_cluster_preflight() -> None:
    with pytest.raises(ValueError, match="skipInClusterPreflight"):
        _parse_azure_provider(
            {
                "enabled": True,
                "defaultSubscriptionId": _SUBSCRIPTION_ID,
                "defaultLocation": _LOCATION,
                "defaultResourceGroup": _RESOURCE_GROUP,
                "identity": _full_payload(),
                "skipInClusterPreflight": True,
            }
        )


def test_infrastructure_providers_round_trip() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=True),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    default_location=_LOCATION,
                    default_resource_group=_RESOURCE_GROUP,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                    ),
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    providers = control_plane["infrastructureProviders"]
    assert providers["docker"] == {"enabled": True}
    assert providers["azure"]["enabled"] is True
    assert providers["azure"]["defaultSubscriptionId"] == _SUBSCRIPTION_ID
    assert providers["azure"]["defaultLocation"] == _LOCATION
    assert providers["azure"]["defaultResourceGroup"] == _RESOURCE_GROUP
    assert providers["azure"]["identity"]["clientId"] == _CLIENT_ID
    assert "azure" not in control_plane
    parsed = ControlPlaneKindConfig.model_validate(control_plane)
    assert isinstance(parsed.infrastructure_providers.docker, DockerInfrastructureProviderConfig)
    assert isinstance(parsed.infrastructure_providers.azure, AzureInfrastructureProviderConfig)


def test_parse_defaults_leave_infrastructure_providers_unspecified() -> None:
    parsed = ControlPlaneKindConfig()

    assert parsed.infrastructure_providers.docker is None
    assert parsed.infrastructure_providers.azure is None


def test_parse_rejects_non_mapping_infrastructure_providers() -> None:
    with pytest.raises(ValueError, match="infrastructureProviders"):
        ControlPlaneKindConfig.model_validate({"infrastructureProviders": "azure"})
