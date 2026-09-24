# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Unit tests for outer stack config routing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stack import (
    _BUILDS_CONFIG,
    CustomRegistryConfig,
    DockerImageBuildConfig,
    CAPZBundleBuildConfig,
    SlinkyChartsBuildConfig,
    _azure_infrastructure_enabled,
    _discover_username,
    _merge_capz_provider_overrides,
    _merge_local_slinky_overrides,
    _slinky_image_config,
    _validate_build_consumers,
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


def test_named_builds_have_typed_builder_options() -> None:
    config = _BUILDS_CONFIG.validate_python({
        "slurm-operator": {
            "builder": "docker-image", "sourcePath": "/src/operator", "sourceRef": "feature",
            "imageName": "custom/operator", "target": "custom-stage", "buildArgs": {"ARCH": "arm64"},
        },
        "capz-provider": {"builder": "capz-bundle", "repositoryUrl": "https://example.invalid/capz.git", "sourceRef": "HEAD"},
        "slinky-charts": {"builder": "slinky-charts", "sourcePath": "/src/operator", "sourceRef": "HEAD"},
    })
    assert isinstance(config["slurm-operator"], DockerImageBuildConfig)
    assert config["slurm-operator"].target == "custom-stage"
    assert config["slurm-operator"].build_args == {"ARCH": "arm64"}
    assert isinstance(config["capz-provider"], CAPZBundleBuildConfig)
    assert isinstance(config["slinky-charts"], SlinkyChartsBuildConfig)
    assert _BUILDS_CONFIG.validate_python({name: build.to_config() for name, build in config.items()}) == config


@pytest.mark.parametrize("options,error", [
    ({}, "union_tag_not_found"),
    ({"builder": "unknown"}, "union_tag_invalid"),
    ({"builder": "docker-image"}, "imageName"),
    ({"builder": "capz-bundle", "target": "stage"}, "extra_forbidden"),
    ({"builder": "docker-image", "imageName": "image", "buildArgs": {"ARCH": 1}}, "string_type"),
])
def test_named_builds_reject_invalid_builder_options(options, error) -> None:
    with pytest.raises(ValidationError, match=error):
        _BUILDS_CONFIG.validate_python({"build": {"sourcePath": "/src/repo", "sourceRef": "HEAD", **options}})


def test_custom_registry_config_is_independent_of_builds() -> None:
    assert CustomRegistryConfig().name == "custom-registry"
    assert CustomRegistryConfig().port is None
    config = CustomRegistryConfig(name="build-registry", port=5002)
    assert config.to_config() == {"name": "build-registry", "port": 5002}
    with pytest.raises(ValidationError):
        CustomRegistryConfig(port=0)


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


@pytest.mark.parametrize("source", [{"sourcePath": "/src/repo"}, {"repositoryUrl": "https://example.invalid/repo.git"}])
@pytest.mark.parametrize("builder", ["docker-image", "capz-bundle", "slinky-charts"])
def test_builds_accept_local_and_remote_sources(source, builder) -> None:
    options = {"imageName": "custom/image"} if builder == "docker-image" else {}
    build = _BUILDS_CONFIG.validate_python({
        "named-build": {"builder": builder, **source, "sourceRef": "feature", **options},
    })["named-build"]
    assert build.source_path == source.get("sourcePath")
    assert build.repository_url == source.get("repositoryUrl")
    assert build.source_ref == "feature"
    if isinstance(build, DockerImageBuildConfig):
        assert build.target is None
        assert build.build_args is None


def test_slinky_deployment_requires_both_image_builds() -> None:
    source = {"sourcePath": "/src/repo", "sourceRef": "HEAD"}
    builds = _BUILDS_CONFIG.validate_python({
        "slurm-operator": {**source, "builder": "docker-image", "imageName": "operator", "target": "custom-stage"},
        "slinky-charts": {**source, "builder": "slinky-charts"},
    })
    with pytest.raises(ValueError, match="slurm-operator-webhook"):
        _validate_build_consumers(builds)
    builds.update(_BUILDS_CONFIG.validate_python({
        "slurm-operator-webhook": {**source, "builder": "docker-image", "imageName": "webhook"},
    }))
    _validate_build_consumers(builds)


def test_build_names_only_select_known_deployment_roles() -> None:
    source = {"sourcePath": "/src/repo", "sourceRef": "HEAD"}
    builds = _BUILDS_CONFIG.validate_python({"standalone-charts": {**source, "builder": "slinky-charts"}})
    _validate_build_consumers(builds)
    builds = _BUILDS_CONFIG.validate_python({"capz-provider": {**source, "builder": "slinky-charts"}})
    with pytest.raises(ValueError, match="requires builder 'capz-bundle'"):
        _validate_build_consumers(builds)


def test_slinky_image_config_parses_registry_port_and_tag() -> None:
    image = _slinky_image_config(
        "custom-registry:5000/slurm-operator:source-1234567890ab"
    )

    assert image is not None
    assert image.repository == "custom-registry:5000/slurm-operator"
    assert image.tag == "source-1234567890ab"


def test_local_slinky_overrides_update_only_local_workload_clusters() -> None:
    config = InitStackConfig(
        tenants=TenantsConfig(
            workload_clusters={
                "local": LocalWorkloadClusterConfig(),
                "caps-aks": AKSWorkloadClusterConfig(
                    parameters=AzureWorkloadSpec(
                        subscription_id=_SUBSCRIPTION_ID,
                        location="westus2",
                        resource_group="rg-capz-mi-dev2",
                    )
                ),
            }
        )
    )

    updated = _merge_local_slinky_overrides(
        config,
        chart_oci_prefix=(
            "oci://custom-registry.pulumi-kubernetes-operator."
            "svc.cluster.local:5000/charts"
        ),
        chart_version="0.0.0-source1234567890ab",
        operator_image=(
            "custom-registry:5000/slurm-operator:source-1234567890ab"
        ),
        webhook_image=(
            "custom-registry:5000/slurm-operator-webhook:source-1234567890ab"
        ),
        registry_name="custom-registry",
        registry_port=5003,
    )

    local = updated.tenants.workload_clusters["local"]
    assert isinstance(local, LocalWorkloadClusterConfig)
    assert local.custom_registry is not None
    assert local.custom_registry.registry_name == "custom-registry"
    assert local.custom_registry.port == 5003
    assert local.slinky.chart_plain_http is True
    assert local.slinky.operator_chart_version == "0.0.0-source1234567890ab"
    assert local.slinky.operator_image is not None
    assert local.slinky.operator_image.repository == (
        "custom-registry:5000/slurm-operator"
    )

    aks = updated.tenants.workload_clusters["caps-aks"]
    assert isinstance(aks, AKSWorkloadClusterConfig)
    assert aks.slinky.operator_image is None


@pytest.mark.parametrize("source,error", [
    ({}, "sourceRef"),
    ({"sourceRef": "HEAD"}, "exactly one"),
    ({"sourcePath": "/src/repo", "repositoryUrl": "https://example.invalid/repo.git", "sourceRef": "HEAD"}, "exactly one"),
])
@pytest.mark.parametrize("builder", ["docker-image", "capz-bundle", "slinky-charts"])
def test_builds_validate_git_sources(source, error, builder) -> None:
    options = {"imageName": "image"} if builder == "docker-image" else {}
    with pytest.raises(ValidationError, match=error):
        _BUILDS_CONFIG.validate_python({"build": {"builder": builder, **source, **options}})


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