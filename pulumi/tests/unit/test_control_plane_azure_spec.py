"""Unit tests for Azure control-plane config parsing and rendering."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from localenv import AzureEnvironment
from stacks.control_plane.control_plane_config import (
    CONTROL_PLANE_KIND_CHILD_CONFIG_KEY,
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
_OTHER_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"
_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"


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
                    "identity": {
                        "type": "UserAssignedMSI",
                        "clientId": _CLIENT_ID,
                        "tenantId": _TENANT_ID,
                    },
                },
            },
            "deployments": {"awx": {"enabled": False}},
        },
    }

    parsed = ControlPlaneKindConfig.model_validate(
        built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    )
    assert parsed.deployments.awx.enabled is False
    azure = _azure_provider(parsed)
    assert azure.identity == _parse_identity(
        {
            "type": "UserAssignedMSI",
            "clientId": _CLIENT_ID,
            "tenantId": _TENANT_ID,
        }
    )
    assert azure.identity.type == "UserAssignedMSI"
    assert azure.default_subscription_id is not None
    assert str(azure.default_subscription_id) == _SUBSCRIPTION_ID


def test_apply_local_environment_discovery_enables_discovered_azure_provider() -> None:
    providers = InfrastructureProvidersConfig().apply_local_environment_discovery(
        infrastructure_providers=("docker", "azure"),
        azure_environment=AzureEnvironment(
            client_ids=(_CLIENT_ID,),
            tenant_id=_TENANT_ID,
            host_subscription_id=_SUBSCRIPTION_ID,
            host_location="westus2",
            host_resource_group="host-rg",
        ),
    )

    assert isinstance(providers.docker, DockerInfrastructureProviderConfig)
    azure = providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    assert str(azure.default_subscription_id) == _SUBSCRIPTION_ID
    assert azure.skip_in_cluster_preflight is False
    assert azure.identity == UserAssignedMSIClusterIdentityConfig(
        client_id=_CLIENT_ID,
        tenant_id=_TENANT_ID,
        allowed_namespaces=[],
    )


def test_apply_local_environment_discovery_preserves_explicit_azure_provider() -> None:
    providers = InfrastructureProvidersConfig(
        azure=AzureInfrastructureProviderConfig(
            enabled=True,
            default_subscription_id=_SUBSCRIPTION_ID,
            identity=UserAssignedMSIClusterIdentityConfig(
                client_id=_CLIENT_ID,
                tenant_id=_TENANT_ID,
            ),
        ),
    ).apply_local_environment_discovery(
        infrastructure_providers=("docker",),
    )

    assert isinstance(providers.docker, DockerInfrastructureProviderConfig)
    assert isinstance(providers.azure, AzureInfrastructureProviderConfig)


def test_apply_local_environment_discovery_leaves_undiscovered_azure_unspecified() -> None:
    providers = InfrastructureProvidersConfig().apply_local_environment_discovery(
        infrastructure_providers=("docker",),
    )

    assert isinstance(providers.docker, DockerInfrastructureProviderConfig)
    assert providers.azure is None


def test_apply_local_environment_discovery_preserves_explicit_disabled_azure() -> None:
    providers = InfrastructureProvidersConfig(
        azure=AzureInfrastructureProviderConfig(enabled=False),
    ).apply_local_environment_discovery(
        infrastructure_providers=("docker", "azure"),
        azure_environment=AzureEnvironment(
            client_ids=(_CLIENT_ID,),
            tenant_id=_TENANT_ID,
            host_subscription_id=_SUBSCRIPTION_ID,
            host_location="westus2",
            host_resource_group="host-rg",
        )
    )

    assert not isinstance(providers.azure, AzureInfrastructureProviderConfig)


def test_build_omits_allowed_namespaces_when_none() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                        allowed_namespaces=None,
                    ),
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    # The CR side treats absence as "allow all"; omitting the key keeps
    # the wire shape minimal and matches what the parser expects.
    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    identity = control_plane["infrastructureProviders"]["azure"]["identity"]
    assert isinstance(identity, dict)
    assert "allowedNamespaces" not in identity


def test_build_serializes_allowed_namespaces_list() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                        allowed_namespaces=["default", "tenant-a"],
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
        "allowedNamespaces": ["default", "tenant-a"],
    }

    parsed = ControlPlaneKindConfig.model_validate(control_plane)
    azure = _azure_provider(parsed)
    assert azure.identity is not None
    assert azure.identity.allowed_namespaces == ["default", "tenant-a"]


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


@pytest.mark.parametrize(
    "missing_key",
    ["clientId", "tenantId"],
)
def test_parse_rejects_missing_guid_field(missing_key: str) -> None:
    payload = _full_payload()
    del payload[missing_key]

    with pytest.raises(ValueError, match=rf"(?s){missing_key}.*Field required"):
        _parse_identity(payload)


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
    payload = _full_payload(allowedNamespaces=["default", ""])

    with pytest.raises(ValueError, match="allowedNamespaces"):
        _parse_identity(payload)


def test_parse_allows_empty_allowed_namespaces_list() -> None:
    # Empty list is semantically "no namespace may reference this
    # identity" per the upstream CRD; we shouldn't *reject* it at parse
    # time because the contract has to round-trip cleanly, but we do
    # surface it as an empty list (not None) so callers can tell.
    payload = _full_payload(allowedNamespaces=[])

    parsed = _parse_identity(payload)
    assert parsed.allowed_namespaces == []


def test_skip_in_cluster_preflight_defaults_to_false() -> None:
    # ``skipInClusterPreflight`` is optional; absent in the wire shape
    # means "run the preflight" so the safe default is False.
    azure = _parse_azure_provider(
        {
            "enabled": True,
            "defaultSubscriptionId": _SUBSCRIPTION_ID,
            "identity": _full_payload(),
        }
    )
    assert azure.skip_in_cluster_preflight is False


def test_apply_local_environment_discovery_completes_partial_azure_provider() -> None:
    providers = InfrastructureProvidersConfig.model_validate(
        {
            "azure": {
                "enabled": True,
                "clientId": _CLIENT_ID,
            }
        }
    ).apply_local_environment_discovery(
        infrastructure_providers=("docker",),
        azure_environment=AzureEnvironment(
            client_ids=(_CLIENT_ID,),
            tenant_id=_TENANT_ID,
            host_subscription_id=_SUBSCRIPTION_ID,
            host_location="westus2",
            host_resource_group="host-rg",
        ),
    )

    azure = providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    assert str(azure.default_subscription_id) == _SUBSCRIPTION_ID
    assert azure.identity == UserAssignedMSIClusterIdentityConfig(
        client_id=_CLIENT_ID,
        tenant_id=_TENANT_ID,
        allowed_namespaces=[],
    )


def test_apply_local_environment_discovery_uses_single_discovered_client_id() -> None:
    providers = InfrastructureProvidersConfig(
        azure=AzureInfrastructureProviderConfig(enabled=True),
    ).apply_local_environment_discovery(
        infrastructure_providers=("docker",),
        azure_environment=AzureEnvironment(
            client_ids=(_CLIENT_ID,),
            tenant_id=_TENANT_ID,
            host_subscription_id=_SUBSCRIPTION_ID,
            host_location="westus2",
            host_resource_group="host-rg",
        ),
    )

    azure = providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    assert azure.identity == UserAssignedMSIClusterIdentityConfig(
        client_id=_CLIENT_ID,
        tenant_id=_TENANT_ID,
        allowed_namespaces=[],
    )


def test_apply_local_environment_discovery_rejects_ambiguous_discovered_client_ids() -> None:
    providers = InfrastructureProvidersConfig(
        azure=AzureInfrastructureProviderConfig(enabled=True),
    )

    with pytest.raises(ValueError, match="exactly one managed identity clientId"):
        providers.apply_local_environment_discovery(
            infrastructure_providers=("docker",),
            azure_environment=AzureEnvironment(
                client_ids=(_CLIENT_ID, _OTHER_CLIENT_ID),
                tenant_id=_TENANT_ID,
                host_subscription_id=_SUBSCRIPTION_ID,
                host_location="westus2",
                host_resource_group="host-rg",
            ),
        )


def test_apply_local_environment_discovery_requires_discovery_for_partial_azure_provider() -> None:
    providers = InfrastructureProvidersConfig.model_validate(
        {
            "azure": {
                "enabled": True,
                "clientId": _CLIENT_ID,
            }
        }
    )

    with pytest.raises(ValueError, match="discovered Azure environment"):
        providers.apply_local_environment_discovery(
            infrastructure_providers=("docker",),
        )


def test_skip_in_cluster_preflight_round_trips_true() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                    ),
                    skip_in_cluster_preflight=True,
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
        "identity": {
            "type": "UserAssignedMSI",
            "clientId": _CLIENT_ID,
            "tenantId": _TENANT_ID,
        },
        "skipInClusterPreflight": True,
    }
    parsed = ControlPlaneKindConfig.model_validate(control_plane)
    assert _azure_provider(parsed).skip_in_cluster_preflight is True


def test_workload_identity_round_trips_and_keeps_preflight_default() -> None:
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
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
    assert azure.skip_in_cluster_preflight is False


def test_build_omits_skip_in_cluster_preflight_when_false() -> None:
    # The default-False case must produce the minimal wire shape so
    # operators can flip the flag back to "use the default" by removing
    # the config key, not by setting it to false explicitly.
    built = _control_plane_child_config(
        ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                docker=DockerInfrastructureProviderConfig(enabled=False),
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                    ),
                    skip_in_cluster_preflight=False,
                ),
            ),
            deployments=ControlPlaneDeploymentsConfig(
                awx=ControlPlaneAWXConfig(enabled=False)
            ),
        )
    )

    control_plane = built[CONTROL_PLANE_KIND_CHILD_CONFIG_KEY]
    assert isinstance(control_plane, dict)
    azure = control_plane["infrastructureProviders"]["azure"]
    assert isinstance(azure, dict)
    assert "skipInClusterPreflight" not in azure


def test_parse_rejects_non_bool_skip_in_cluster_preflight() -> None:
    with pytest.raises(ValueError, match="skipInClusterPreflight"):
        _parse_azure_provider(
            {
                "enabled": True,
                "defaultSubscriptionId": _SUBSCRIPTION_ID,
                "identity": _full_payload(),
                "skipInClusterPreflight": "yes",
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
