"""Unit tests for outer stack config routing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stack import (
    CAPZArtifactConfig,
    CustomImagesConfig,
    _azure_infrastructure_enabled,
    _discover_username,
    _merge_capz_provider_overrides,
    _with_local_registry_config,
    _with_owner_tag_config,
)
from stacks.control_plane.control_plane_config import (
    AllowedNamespacesConfig,
    AzureInfrastructureProviderConfig,
    ControlPlaneKindConfig,
    InfrastructureProvidersConfig,
    UserAssignedMSIClusterIdentityConfig,
)
from stacks.init.init_stack import InitStackConfig
from stacks.workload_cluster.registry_setting import LocalPortRegistrySetting
from stacks.workload_cluster.tenants import TenantsConfig
from stacks.workload_cluster.workload_cluster_class_aks import (
    AKSWorkloadClusterConfig,
    AzureWorkloadSpec,
)
from stacks.workload_cluster.workload_cluster_class_azure_byo import (
    AzureBYOWorkloadClusterConfig,
    AzureBYOWorkloadSpec,
)
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig


_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"
_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"
_LOCATION = "westus2"
_RESOURCE_GROUP = "host-rg"


def test_empty_config_does_not_enable_azure() -> None:
    assert not _azure_infrastructure_enabled(InitStackConfig())


def test_azure_enabled_config_enables_azure() -> None:
    config = InitStackConfig(
        control_plane=ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
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
        ),
    )

    assert _azure_infrastructure_enabled(config)


def test_empty_stack_config_keeps_azure_disabled() -> None:
    config = InitStackConfig.model_validate({})

    assert not _azure_infrastructure_enabled(config)


def test_custom_images_config_parses_source_and_registry_options() -> None:
    config = CustomImagesConfig.model_validate(
        {
            "registryName": "images-registry",
            "registryPort": 5002,
            "images": {
                "controller": {
                    "sourcePath": "/src/controller",
                    "sourceRef": "feature",
                    "imageName": "custom/controller",
                    "buildArgs": {"ARCH": "amd64", "package": "./cmd"},
                }
            },
        }
    )

    image = config.images["controller"]
    assert config.registry_name == "images-registry"
    assert config.registry_port == 5002
    assert image.source_path == "/src/controller"
    assert image.source_ref == "feature"
    assert image.image_name == "custom/controller"
    assert image.build_args == {"ARCH": "amd64", "package": "./cmd"}


def test_custom_images_config_defaults_to_dedicated_registry() -> None:
    config = CustomImagesConfig.model_validate(
        {
            "images": {
                "controller": {
                    "sourcePath": "/src/controller",
                    "sourceRef": "HEAD",
                    "imageName": "custom/controller",
                }
            },
        }
    )

    image = config.images["controller"]
    assert config.registry_name == "custom-registry"
    assert config.registry_port is None
    assert image.build_args is None


def test_capz_artifact_config_defaults_to_capz_artifact_name() -> None:
    config = CAPZArtifactConfig.model_validate(
        {
            "sourcePath": "/src/capz",
            "sourceRef": "origin/arsdragonfly/md-vmss",
        }
    )

    assert config.source_path == "/src/capz"
    assert config.source_ref == "origin/arsdragonfly/md-vmss"
    assert config.artifact_name == "capz/cluster-api-provider-azure"


@pytest.mark.parametrize(
    "value, expected_errors",
    [
        (
            {},
            {
                (("sourcePath",), "missing"),
                (("sourceRef",), "missing"),
            },
        ),
        (
            {
                "sourcePath": "/src/capz",
                "sourceRef": "HEAD",
                "artifactName": "",
            },
            {(("artifactName",), "string_too_short")},
        ),
    ],
)
def test_capz_artifact_config_rejects_invalid_values(
    value: object,
    expected_errors: set[tuple[tuple[str, ...], str]],
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        CAPZArtifactConfig.model_validate(value)

    errors = {
        (tuple(str(part) for part in error["loc"]), str(error["type"]))
        for error in exc_info.value.errors()
    }
    assert expected_errors <= errors


@pytest.mark.parametrize(
    "value, expected_errors",
    [
        (
            {"registryPort": 0, "images": {}},
            {(('registryPort',), 'greater_than')},
        ),
        (
            {"images": {"controller": {}}},
            {
                (('images', 'controller', 'sourcePath'), 'missing'),
                (('images', 'controller', 'sourceRef'), 'missing'),
                (('images', 'controller', 'imageName'), 'missing'),
            },
        ),
        (
            {
                "images": {
                    "controller": {
                        "sourcePath": "/src/controller",
                        "sourceRef": "HEAD",
                        "imageName": "custom/controller",
                        "buildArgs": [],
                    }
                }
            },
            {(('images', 'controller', 'buildArgs'), 'dict_type')},
        ),
        (
            {
                "images": {
                    "controller": {
                        "sourcePath": "/src/controller",
                        "sourceRef": "HEAD",
                        "imageName": "custom/controller",
                        "buildArgs": {"ARCH": 1},
                    }
                }
            },
            {(('images', 'controller', 'buildArgs', 'ARCH'), 'string_type')},
        ),
    ],
)
def test_custom_images_config_rejects_invalid_values(
    value: object,
    expected_errors: set[tuple[tuple[str, ...], str]],
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        CustomImagesConfig.model_validate(value)

    errors = {
        (tuple(str(part) for part in error["loc"]), str(error["type"]))
        for error in exc_info.value.errors()
    }
    assert expected_errors <= errors


def test_explicit_stack_config_enables_azure() -> None:
    config = InitStackConfig.model_validate(
        {
            "controlPlane": {
                "infrastructureProviders": {
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
                    }
                },
                "deployments": {"awx": {"enabled": False}},
            },
            "tenants": {
                "workloadClusters": {
                    "caps-aks": {
                        "className": "aks",
                        "parameters": {
                            "location": _LOCATION,
                            "resourceGroup": _RESOURCE_GROUP,
                            "additionalTags": {"owner": "platform"},
                        },
                    },
                },
            },
        }
    )

    azure = config.control_plane.infrastructure_providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    assert str(azure.default_subscription_id) == _SUBSCRIPTION_ID
    assert azure.default_location == _LOCATION
    assert azure.default_resource_group == _RESOURCE_GROUP
    assert isinstance(azure.identity, UserAssignedMSIClusterIdentityConfig)
    assert str(azure.identity.client_id) == _CLIENT_ID
    assert str(azure.identity.tenant_id) == _TENANT_ID
    assert azure.identity.allowed_namespaces == AllowedNamespacesConfig()

    workload_cluster = config.tenants.workload_clusters["caps-aks"]
    assert isinstance(workload_cluster, AKSWorkloadClusterConfig)
    assert workload_cluster.parameters.location == _LOCATION
    assert workload_cluster.parameters.resource_group == _RESOURCE_GROUP
    assert workload_cluster.parameters.additional_tags == {"owner": "platform"}


def test_local_registry_config_is_applied_to_local_workload_clusters_only() -> None:
    config = InitStackConfig(
        tenants=TenantsConfig(
            workload_clusters={
                "local": LocalWorkloadClusterConfig(),
                "caps-aks": AKSWorkloadClusterConfig(
                    parameters=AzureWorkloadSpec(
                        subscription_id=_SUBSCRIPTION_ID,
                        location="westus2",
                        resource_group="rg-capz-mi-dev2",
                        additional_tags={},
                    )
                ),
            }
        )
    )

    updated = _with_local_registry_config(
        config,
        LocalPortRegistrySetting(port=5002),
    )

    assert updated.tenants.to_config() == {
        "workloadClusters": {
            "local": {
                "className": "local",
                "registry": {"kind": "local-port", "port": 5002},
            },
            "caps-aks": {
                "className": "aks",
                "parameters": {
                    "subscriptionId": _SUBSCRIPTION_ID,
                    "location": "westus2",
                    "resourceGroup": "rg-capz-mi-dev2",
                    "additionalTags": {},
                },
            },
        }
    }


def test_owner_tag_config_is_applied_to_azure_workload_clusters_only() -> None:
    config = InitStackConfig(
        tenants=TenantsConfig(
            workload_clusters={
                "local": LocalWorkloadClusterConfig(),
                "caps-aks": AKSWorkloadClusterConfig(
                    parameters=AzureWorkloadSpec(
                        subscription_id=_SUBSCRIPTION_ID,
                        location="westus2",
                        resource_group="rg-capz-mi-dev2",
                        additional_tags={"costCenter": "hpc"},
                    )
                ),
                "caps-self": AzureBYOWorkloadClusterConfig(
                    parameters=AzureBYOWorkloadSpec(
                        subscription_id=_SUBSCRIPTION_ID,
                        location="southcentralus",
                        additional_tags={"costCenter": "hpc"},
                    )
                ),
            }
        )
    )

    updated = _with_owner_tag_config(config, owner="zheyushen")

    assert updated.tenants.to_config() == {
        "workloadClusters": {
            "local": {"className": "local"},
            "caps-aks": {
                "className": "aks",
                "parameters": {
                    "subscriptionId": _SUBSCRIPTION_ID,
                    "location": "westus2",
                    "resourceGroup": "rg-capz-mi-dev2",
                    "additionalTags": {
                        "costCenter": "hpc",
                        "Owner": "zheyushen",
                    },
                },
            },
            "caps-self": {
                "className": "azure-byo",
                "parameters": {
                    "subscriptionId": _SUBSCRIPTION_ID,
                    "location": "southcentralus",
                    "additionalTags": {
                        "costCenter": "hpc",
                        "Owner": "zheyushen",
                    },
                },
            },
        }
    }


def test_owner_tag_config_preserves_explicit_owner_tag() -> None:
    config = InitStackConfig(
        tenants=TenantsConfig(
            workload_clusters={
                "caps-aks": AKSWorkloadClusterConfig(
                    parameters=AzureWorkloadSpec(
                        subscription_id=_SUBSCRIPTION_ID,
                        location="westus2",
                        resource_group="rg-capz-mi-dev2",
                        additional_tags={"owner": "platform"},
                    )
                ),
            }
        )
    )

    updated = _with_owner_tag_config(config, owner="zheyushen")

    workload_cluster = updated.tenants.workload_clusters["caps-aks"]
    assert isinstance(workload_cluster, AKSWorkloadClusterConfig)
    assert workload_cluster.parameters.additional_tags == {"owner": "platform"}


def test_capz_provider_overrides_are_applied_to_enabled_azure_provider() -> None:
    config = InitStackConfig(
        control_plane=ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                azure=AzureInfrastructureProviderConfig(
                    enabled=True,
                    default_subscription_id=_SUBSCRIPTION_ID,
                    default_location=_LOCATION,
                    default_resource_group=_RESOURCE_GROUP,
                    identity=UserAssignedMSIClusterIdentityConfig(
                        client_id=_CLIENT_ID,
                        tenant_id=_TENANT_ID,
                    ),
                )
            )
        )
    )

    updated = _merge_capz_provider_overrides(
        config,
        provider_oci=(
            "http://custom-registry.pulumi-kubernetes-operator.svc.cluster.local:5000/"
            "capz/cluster-api-provider-azure:source-1234567890ab"
        ),
        controller_image=(
            "custom-registry:5000/capz/cluster-api-azure-controller:source-1234567890ab"
        ),
    )

    azure = updated.control_plane.infrastructure_providers.azure
    assert isinstance(azure, AzureInfrastructureProviderConfig)
    assert azure.provider_oci == (
        "http://custom-registry.pulumi-kubernetes-operator.svc.cluster.local:5000/"
        "capz/cluster-api-provider-azure:source-1234567890ab"
    )
    assert azure.controller_image == (
        "custom-registry:5000/capz/cluster-api-azure-controller:source-1234567890ab"
    )
    assert updated.control_plane.to_config()["infrastructureProviders"]["azure"][
        "providerOci"
    ] == (
        "http://custom-registry.pulumi-kubernetes-operator.svc.cluster.local:5000/"
        "capz/cluster-api-provider-azure:source-1234567890ab"
    )


def test_capz_provider_overrides_skip_disabled_azure_provider() -> None:
    config = InitStackConfig(
        control_plane=ControlPlaneKindConfig(
            infrastructure_providers=InfrastructureProvidersConfig(
                azure=AzureInfrastructureProviderConfig(enabled=False)
            )
        )
    )

    assert _merge_capz_provider_overrides(
        config,
        provider_oci="http://custom-registry/capz:tag",
        controller_image="custom-registry:5000/capz/controller:tag",
    ) == config


def test_discover_username_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", " zheyushen ")
    monkeypatch.setenv("LOGNAME", "someone-else")

    assert _discover_username() == "zheyushen"


def test_discover_username_falls_back_to_logname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("LOGNAME", "zheyushen")

    assert _discover_username() == "zheyushen"