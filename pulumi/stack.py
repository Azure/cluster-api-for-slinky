# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Outer stack body for Kind-backed management clusters."""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping
from typing import Annotated, Literal

import pulumi
import pulumi_kubernetes as k8s
from pydantic import Field, TypeAdapter, model_validator

from artifacts import PublishedArtifacts, RegistryDestination
from artifacts import capz, image, slinky
from azure_container_registry import AzureContainerRegistryConfig, EphemeralAzureContainerRegistry
from ctlptl import (
    CloudProviderKind,
    CloudProviderKindConfig,
    CtlptlCluster,
    CtlptlRegistry,
    CtlptlRegistryService,
)
from gitrepo import GitOpsConfig, GitOpsRepository, GitOpsWebhook
from fluxcd import FluxInfrastructure
from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from pko import PKOBootstrap, PKO_NAMESPACE
from stacks.workload_cluster.registry_setting import (
    LocalCustomRegistrySetting,
    LocalPortRegistrySetting,
)
from stacks.workload_cluster.tenants import (
    WorkloadClusterConfig,
)
from stacks.init.init_stack import InitStackConfig
from stacks.workload_cluster.workload_cluster_class_aks import AKSWorkloadClusterConfig
from stacks.workload_cluster.workload_cluster_class_azure_byo import (
    AzureBYOWorkloadClusterConfig,
)
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig
from stacks.workload_cluster.workload_cluster_deployments import (
    SlinkyDeploymentConfig,
    SlinkyImageConfig,
)


_OWNER_TAG = "Owner"
_CUSTOM_REGISTRY_NAME = "custom-registry"
_CUSTOM_REGISTRY_ENV: list[pulumi.Input[str]] = [
    "CA4S_REGISTRY_MODE=custom-registry",
]
_CAPZ_CONTROLLER_IMAGE_KEY = "capz-controller"
_CAPZ_PROVIDER_BUILD_KEY = "capz-provider"
_SLINKY_CHARTS_BUILD_KEY = "slinky-charts"
_SLINKY_OPERATOR_IMAGE_KEY = "slurm-operator"
_SLINKY_WEBHOOK_IMAGE_KEY = "slurm-operator-webhook"


class GitSourceConfig(PulumiConfigModel):
    source_path: NonEmptyStr | None = None
    repository_url: NonEmptyStr | None = None
    source_ref: NonEmptyStr

    @model_validator(mode="after")
    def validate_source(self) -> GitSourceConfig:
        if (self.source_path is None) == (self.repository_url is None):
            raise ValueError("exactly one of sourcePath or repositoryUrl is required")
        return self


class DockerImageBuildConfig(GitSourceConfig):
    builder: Literal["docker-image"]
    image_name: NonEmptyStr
    target: NonEmptyStr | None = None
    build_args: Mapping[NonEmptyStr, NonEmptyStr] | None = None


class CAPZBundleBuildConfig(GitSourceConfig):
    builder: Literal["capz-bundle"]


class SlinkyChartsBuildConfig(GitSourceConfig):
    builder: Literal["slinky-charts"]


BuildConfig = Annotated[
    DockerImageBuildConfig | CAPZBundleBuildConfig | SlinkyChartsBuildConfig,
    Field(discriminator="builder"),
]
_BUILDS_CONFIG = TypeAdapter(dict[NonEmptyStr, BuildConfig])


class CustomRegistryConfig(PulumiConfigModel):
    name: NonEmptyStr = _CUSTOM_REGISTRY_NAME
    port: StrictPositiveInt | None = None


_BUILDERS = {
    "docker-image": image,
    "capz-bundle": capz,
    "slinky-charts": slinky,
}


def _validate_build_consumers(builds: Mapping[str, BuildConfig]) -> None:
    roles = {
        _CAPZ_CONTROLLER_IMAGE_KEY: "docker-image",
        _CAPZ_PROVIDER_BUILD_KEY: "capz-bundle",
        _SLINKY_OPERATOR_IMAGE_KEY: "docker-image",
        _SLINKY_WEBHOOK_IMAGE_KEY: "docker-image",
        _SLINKY_CHARTS_BUILD_KEY: "slinky-charts",
    }
    for name, builder in roles.items():
        if name in builds and builds[name].builder != builder:
            raise ValueError(f"deployment build {name!r} requires builder {builder!r}")
    if _SLINKY_CHARTS_BUILD_KEY in builds:
        missing = {_SLINKY_OPERATOR_IMAGE_KEY, _SLINKY_WEBHOOK_IMAGE_KEY}.difference(builds)
        if missing:
            raise ValueError("slinky-charts requires builds: " + ", ".join(sorted(missing)))


def run_stack() -> None:
    """Build the Kind management-cluster graph from config and discovery."""
    config = pulumi.Config()
    cloud_provider_kind_config = CloudProviderKindConfig.model_validate(
        config.get_object("cloudProviderKind") or {}
    )
    gitops_config = GitOpsConfig.model_validate(config.get_object("gitops") or {})
    builds_config = _BUILDS_CONFIG.validate_python(config.get_object("builds") or {})
    registry_config = CustomRegistryConfig.model_validate(config.get_object("customRegistry") or {})
    _validate_build_consumers(builds_config)

    configured_init_stack = _with_owner_tag_config(
        InitStackConfig.model_validate(config.get_object("initStack") or {})
    )
    workloads = configured_init_stack.tenants.workload_clusters.values()
    azure_workloads = [workload for workload in workloads if isinstance(
        workload, (AKSWorkloadClusterConfig, AzureBYOWorkloadClusterConfig)
    )]
    has_local_workloads = any(isinstance(workload, LocalWorkloadClusterConfig) for workload in workloads)
    azure_registry = None
    destinations: dict[str, RegistryDestination] = {}
    registry_dependencies: dict[str, pulumi.Resource] = {}
    if azure_workloads:
        placement = azure_workloads[0].parameters
        azure_provider_config = configured_init_stack.control_plane.infrastructure_providers.azure
        runner_identity = azure_provider_config.identity if azure_provider_config is not None else None
        azure_registry = EphemeralAzureContainerRegistry(
            "workload-registry", subscription_id=str(placement.subscription_id),
            location=placement.location, tags=dict(placement.additional_tags),
            runner_identity_resource_id=runner_identity.resource_id if runner_identity is not None else None,
        )
        destinations["acr"] = {
            "server": azure_registry.server, "consumer_server": azure_registry.server,
            "plain_http": False, "acr_resource_id": azure_registry.resource_id,
        }
        registry_dependencies["acr"] = azure_registry
    slinky_image_keys = {_SLINKY_OPERATOR_IMAGE_KEY, _SLINKY_WEBHOOK_IMAGE_KEY}
    build_destinations = {
        name: (
            (["local", "acr"] if has_local_workloads else ["acr"])
            if azure_registry is not None and name in slinky_image_keys else ["local"]
        )
        for name in builds_config
    }
    cache_registry = CtlptlRegistry("cache-registry")
    custom_registry: CtlptlRegistry | None = None
    if any("local" in targets for targets in build_destinations.values()):
        custom_registry = CtlptlRegistry(
            "custom-registry",
            registry_name=registry_config.name,
            port=registry_config.port,
            env=_CUSTOM_REGISTRY_ENV,
        )
        destinations["local"] = {
            "server": custom_registry.port.apply(lambda port: f"localhost:{int(port)}"),
            "consumer_server": pulumi.Output.concat(custom_registry.registry_name, ":5000"),
            "plain_http": True,
        }
        registry_dependencies["local"] = custom_registry
    published_builds: dict[str, PublishedArtifacts] = {}
    azure_builds: dict[str, PublishedArtifacts] = {}
    publications: dict[str, dict[str, PublishedArtifacts]] = {}
    for build_name, build_config in builds_config.items():
        publications[build_name] = {}
        for target in build_destinations[build_name]:
            builder = _BUILDERS[build_config.builder]
            build_options = (
                build_config.model_dump(include={"image_name", "target", "build_args"})
                if isinstance(build_config, DockerImageBuildConfig) else None
            )
            publication = PublishedArtifacts(
                f"build-{build_name}" if target == "local" else f"build-{build_name}-acr",
                plan=builder.artifact_tags,
                build=builder.build_and_publish,
                source_path=build_config.source_path,
                repository_url=build_config.repository_url,
                source_ref=build_config.source_ref,
                destination=destinations[target],
                build_options=build_options,
                resource_id_repository=(
                    slinky.RESOURCE_ID_REPOSITORY
                    if isinstance(build_config, SlinkyChartsBuildConfig) else None
                ),
                opts=pulumi.ResourceOptions(depends_on=[registry_dependencies[target]]),
            )
            publications[build_name][target] = publication
            (published_builds if target == "local" else azure_builds)[build_name] = publication
    if publications:
        pulumi.export("build_artifact_refs", {
            name: {target: build.artifact_refs for target, build in targets.items()}
            for name, targets in publications.items()
        })

    custom_registry_names: list[pulumi.Input[str]] | None = None
    if custom_registry is not None:
        custom_registry_names = [custom_registry.registry_name]
    cluster = CtlptlCluster(
        "mgmt",
        registry_name=cache_registry.registry_name,
        custom_registry_names=custom_registry_names,
    )
    mgmt_provider = k8s.Provider(
        "mgmt-k8s",
        kubeconfig=cluster.kubeconfig,
        opts=pulumi.ResourceOptions(depends_on=[cluster]),
    )

    pko_namespace = k8s.core.v1.Namespace(
        "pko-ns",
        metadata={"name": PKO_NAMESPACE},
        opts=pulumi.ResourceOptions(provider=mgmt_provider),
    )
    custom_registry_service: CtlptlRegistryService | None = None
    if custom_registry is not None:
        custom_registry_service = CtlptlRegistryService(
            "custom-registry-service",
            registry_name=custom_registry.registry_name,
            namespace=pko_namespace.metadata["name"],
            provider=mgmt_provider,
            dependencies=[cluster, custom_registry, pko_namespace],
        )
    capz_artifact = published_builds.get(_CAPZ_PROVIDER_BUILD_KEY)
    capz_artifact_oci_url: pulumi.Output[str] | None = None
    if capz_artifact is not None and custom_registry_service is not None:
        capz_artifact_oci_url = _capz_artifact_oci_url(
            capz_artifact=capz_artifact,
            custom_registry_service=custom_registry_service,
        )

    lb = CloudProviderKind(
        "lb",
        config=cloud_provider_kind_config,
    )
    flux = FluxInfrastructure(
        "flux",
        provider=mgmt_provider,
        artifact_consumer_namespaces=[PKO_NAMESPACE],
    )

    repo = GitOpsRepository(
        "gitops",
        config=gitops_config,
        runtime_args={
            "kubeconfig": cluster.kubeconfig,
            "flux_provider": mgmt_provider,
            "flux_infrastructure": flux,
            "flux_source_namespace": pko_namespace.metadata["name"],
            "flux_source_namespace_resource": pko_namespace,
        },
    )

    base_init_stack_config = _with_local_registry_config(
        configured_init_stack, LocalPortRegistrySetting(port=cache_registry.port),
    )
    capz_controller_image_ref: pulumi.Output[str] | None = None
    if _CAPZ_CONTROLLER_IMAGE_KEY in published_builds:
        capz_controller_image_ref = published_builds[_CAPZ_CONTROLLER_IMAGE_KEY].artifact_refs.apply(
            lambda references: next(iter(references.values()))
        )
    init_stack_config = _with_capz_provider_overrides(
        base_init_stack_config,
        provider_oci=capz_artifact_oci_url,
        controller_image=capz_controller_image_ref,
    )
    slinky_operator_image_ref: pulumi.Output[str] | None = None
    if _SLINKY_OPERATOR_IMAGE_KEY in published_builds:
        slinky_operator_image_ref = published_builds[_SLINKY_OPERATOR_IMAGE_KEY].artifact_refs.apply(
            lambda references: next(iter(references.values()))
        )
    slinky_webhook_image_ref: pulumi.Output[str] | None = None
    if _SLINKY_WEBHOOK_IMAGE_KEY in published_builds:
        slinky_webhook_image_ref = published_builds[_SLINKY_WEBHOOK_IMAGE_KEY].artifact_refs.apply(
            lambda references: next(iter(references.values()))
        )
    slinky_charts = published_builds.get(_SLINKY_CHARTS_BUILD_KEY)
    slinky_chart_oci_prefix: pulumi.Output[str] | None = None
    if slinky_charts is not None and custom_registry_service is not None:
        slinky_chart_oci_prefix = _slinky_chart_oci_prefix(custom_registry_service)
    has_local_slinky_overrides = any(
        item is not None
        for item in (
            slinky_charts,
            slinky_operator_image_ref,
            slinky_webhook_image_ref,
        )
    )
    init_stack_config = _with_local_slinky_overrides(
        init_stack_config,
        chart_oci_prefix=slinky_chart_oci_prefix,
        chart_version=(slinky_charts.artifact_tags["charts/slurm"] if slinky_charts is not None else None),
        operator_image=slinky_operator_image_ref,
        webhook_image=slinky_webhook_image_ref,
        registry_name=registry_config.name if has_local_slinky_overrides else None,
        registry_port=(custom_registry.port if custom_registry is not None and has_local_slinky_overrides else None),
    )
    if azure_registry is not None:
        init_stack_config = pulumi.Output.all(
            config=init_stack_config,
            acr=azure_registry.config,
            image_refs={name: build.artifact_refs for name, build in azure_builds.items()},
            chart_oci_prefix=slinky_chart_oci_prefix,
            chart_version=slinky_charts.artifact_tags["charts/slurm"] if slinky_charts is not None else None,
        ).apply(lambda values: _merge_azure_build_overrides(**values))

    pko = PKOBootstrap(
        "pko",
        provider=mgmt_provider,
        namespace_resource=pko_namespace,
        flux_source=repo.flux_source,
        env=pulumi.get_stack(),
        init_stack_config=init_stack_config,
    )

    gitops_webhook = GitOpsWebhook(
        "gitops-flux-webhook",
        config=gitops_config,
        gitops_webhook_args=repo.webhook_args,
        opts=pulumi.ResourceOptions(depends_on=[pko]),
    )

    _export_common_outputs(
        cache_registry=cache_registry,
        cluster=cluster,
        lb=lb,
        repo=repo,
        gitops_provider=gitops_config.provider,
        gitops_webhook=gitops_webhook,
        pko=pko,
    )
    if _azure_infrastructure_enabled(base_init_stack_config):
        _export_azure_config_outputs(base_init_stack_config)


def _merge_azure_build_overrides(
    *, config: InitStackConfig, acr: AzureContainerRegistryConfig,
    image_refs: dict[str, dict[str, str]], chart_oci_prefix: str | None, chart_version: str | None,
) -> InitStackConfig:
    workloads = {}
    for name, workload in config.tenants.workload_clusters.items():
        if isinstance(workload, (AKSWorkloadClusterConfig, AzureBYOWorkloadClusterConfig)):
            updates = {}
            for build_name, field in (
                (_SLINKY_OPERATOR_IMAGE_KEY, "operator_image"),
                (_SLINKY_WEBHOOK_IMAGE_KEY, "webhook_image"),
            ):
                if build_name in image_refs:
                    updates[field] = _slinky_image_config(next(iter(image_refs[build_name].values())))
            if chart_oci_prefix is not None:
                updates.update(chart_oci_prefix=chart_oci_prefix, chart_plain_http=True)
            if chart_version is not None:
                updates.update(
                    operator_crds_chart_version=chart_version,
                    operator_chart_version=chart_version, slurm_chart_version=chart_version,
                )
            workload = workload.model_copy(update={"acr": acr, "slinky": workload.slinky.model_copy(update=updates)})
        workloads[name] = workload
    return config.model_copy(update={"tenants": config.tenants.model_copy(update={"workload_clusters": workloads})})


def _with_local_registry_config(
    init_stack_config: InitStackConfig,
    registry: LocalPortRegistrySetting,
) -> InitStackConfig:
    workload_clusters: dict[str, WorkloadClusterConfig] = {}
    for name, workload_cluster in init_stack_config.tenants.workload_clusters.items():
        if isinstance(workload_cluster, LocalWorkloadClusterConfig):
            workload_cluster = workload_cluster.model_copy(
                update={"registry": registry}
            )
        workload_clusters[name] = workload_cluster

    return init_stack_config.model_copy(
        update={
            "tenants": init_stack_config.tenants.model_copy(
                update={"workload_clusters": workload_clusters}
            )
        }
    )


def _slinky_image_config(image_ref: str | None) -> SlinkyImageConfig | None:
    if image_ref is None:
        return None
    repository, separator, tag = image_ref.rpartition(":")
    if not separator or not repository or not tag:
        raise ValueError(f"invalid tagged Slinky image reference: {image_ref!r}")
    return SlinkyImageConfig(repository=repository, tag=tag)


def _merge_local_slinky_overrides(
    init_stack_config: InitStackConfig,
    *,
    chart_oci_prefix: str | None,
    chart_version: str | None,
    operator_image: str | None,
    webhook_image: str | None,
    registry_name: str | None,
    registry_port: int | None,
) -> InitStackConfig:
    workload_clusters: dict[str, WorkloadClusterConfig] = {}
    for name, workload_cluster in init_stack_config.tenants.workload_clusters.items():
        if isinstance(workload_cluster, LocalWorkloadClusterConfig):
            slinky_updates: dict[str, object] = {}
            if chart_oci_prefix is not None:
                slinky_updates.update(
                    {
                        "chart_oci_prefix": chart_oci_prefix,
                        "chart_plain_http": True,
                    }
                )
            if chart_version is not None:
                slinky_updates.update(
                    {
                        "operator_crds_chart_version": chart_version,
                        "operator_chart_version": chart_version,
                        "slurm_chart_version": chart_version,
                    }
                )
            resolved_operator_image = _slinky_image_config(operator_image)
            if resolved_operator_image is not None:
                slinky_updates["operator_image"] = resolved_operator_image
            resolved_webhook_image = _slinky_image_config(webhook_image)
            if resolved_webhook_image is not None:
                slinky_updates["webhook_image"] = resolved_webhook_image

            cluster_updates: dict[str, object] = {}
            if slinky_updates:
                cluster_updates["slinky"] = workload_cluster.slinky.model_copy(
                    update=slinky_updates
                )
            if registry_name is not None and registry_port is not None:
                cluster_updates["custom_registry"] = LocalCustomRegistrySetting(
                    registry_name=registry_name,
                    port=registry_port,
                )
            if cluster_updates:
                workload_cluster = workload_cluster.model_copy(update=cluster_updates)
        workload_clusters[name] = workload_cluster

    return init_stack_config.model_copy(
        update={
            "tenants": init_stack_config.tenants.model_copy(
                update={"workload_clusters": workload_clusters}
            )
        }
    )


def _with_local_slinky_overrides(
    init_stack_config: pulumi.Input[InitStackConfig],
    *,
    chart_oci_prefix: pulumi.Input[str] | None,
    chart_version: pulumi.Input[str] | None,
    operator_image: pulumi.Input[str] | None,
    webhook_image: pulumi.Input[str] | None,
    registry_name: pulumi.Input[str] | None,
    registry_port: pulumi.Input[int] | None,
) -> pulumi.Input[InitStackConfig]:
    if all(
        item is None
        for item in (
            chart_oci_prefix,
            chart_version,
            operator_image,
            webhook_image,
            registry_name,
            registry_port,
        )
    ):
        return init_stack_config

    def merge(resolved: dict[str, object]) -> InitStackConfig:
        config_value = resolved["init_stack_config"]
        resolved_config = (
            config_value
            if isinstance(config_value, InitStackConfig)
            else InitStackConfig.model_validate(config_value)
        )
        return _merge_local_slinky_overrides(
            resolved_config,
            chart_oci_prefix=resolved.get("chart_oci_prefix"),
            chart_version=resolved.get("chart_version"),
            operator_image=resolved.get("operator_image"),
            webhook_image=resolved.get("webhook_image"),
            registry_name=resolved.get("registry_name"),
            registry_port=resolved.get("registry_port"),
        )

    return pulumi.Output.all(
        init_stack_config=init_stack_config,
        chart_oci_prefix=chart_oci_prefix,
        chart_version=chart_version,
        operator_image=operator_image,
        webhook_image=webhook_image,
        registry_name=registry_name,
        registry_port=registry_port,
    ).apply(merge)


def _with_owner_tag_config(
    init_stack_config: InitStackConfig,
    owner: str | None = None,
) -> InitStackConfig:
    owner = owner or _discover_username()
    if owner is None:
        return init_stack_config

    workload_clusters: dict[str, WorkloadClusterConfig] = {}
    for name, workload_cluster in init_stack_config.tenants.workload_clusters.items():
        if isinstance(
            workload_cluster,
            (AKSWorkloadClusterConfig, AzureBYOWorkloadClusterConfig),
        ):
            parameters = workload_cluster.parameters
            additional_tags = dict(parameters.additional_tags)
            if not _has_owner_tag(additional_tags):
                workload_cluster = workload_cluster.model_copy(
                    update={
                        "parameters": parameters.model_copy(
                            update={
                                "additional_tags": {
                                    **additional_tags,
                                    _OWNER_TAG: owner,
                                }
                            }
                        )
                    }
                )
        workload_clusters[name] = workload_cluster

    return init_stack_config.model_copy(
        update={
            "tenants": init_stack_config.tenants.model_copy(
                update={"workload_clusters": workload_clusters}
            )
        }
    )


def _discover_username() -> str | None:
    for env_var in ("USER", "LOGNAME", "USERNAME"):
        value = os.environ.get(env_var)
        if value is not None and value.strip():
            return value.strip()

    try:
        value = getpass.getuser()
    except Exception:
        return None
    return value.strip() or None


def _has_owner_tag(additional_tags: dict[str, str]) -> bool:
    return any(tag.casefold() == _OWNER_TAG.casefold() for tag in additional_tags)


def _merge_capz_provider_overrides(
    init_stack_config: InitStackConfig,
    *,
    provider_oci: str | None,
    controller_image: str | None,
) -> InitStackConfig:
    if provider_oci is None and controller_image is None:
        return init_stack_config

    control_plane = init_stack_config.control_plane
    providers = control_plane.infrastructure_providers
    azure_provider = providers.azure
    if azure_provider is None or not azure_provider.enabled:
        return init_stack_config

    updates: dict[str, str] = {}
    if provider_oci is not None:
        updates["provider_oci"] = provider_oci
    if controller_image is not None:
        updates["controller_image"] = controller_image

    return init_stack_config.model_copy(
        update={
            "control_plane": control_plane.model_copy(
                update={
                    "infrastructure_providers": providers.model_copy(
                        update={
                            "azure": azure_provider.model_copy(update=updates),
                        }
                    )
                }
            )
        }
    )


def _with_capz_provider_overrides(
    init_stack_config: InitStackConfig,
    *,
    provider_oci: pulumi.Input[str] | None,
    controller_image: pulumi.Input[str] | None,
) -> pulumi.Input[InitStackConfig]:
    if provider_oci is None and controller_image is None:
        return init_stack_config

    return pulumi.Output.all(
        provider_oci=provider_oci,
        controller_image=controller_image,
    ).apply(
        lambda resolved: _merge_capz_provider_overrides(
            init_stack_config,
            provider_oci=resolved.get("provider_oci"),
            controller_image=resolved.get("controller_image"),
        )
    )


def _capz_artifact_oci_url(
    *,
    capz_artifact: PublishedArtifacts,
    custom_registry_service: CtlptlRegistryService,
) -> pulumi.Output[str]:
    return pulumi.Output.concat(
        custom_registry_service.url,
        "/",
        capz_artifact.artifact_tags.apply(
            lambda tags: ":".join(next(iter(tags.items())))
        ),
    )


def _slinky_chart_oci_prefix(
    custom_registry_service: CtlptlRegistryService,
) -> pulumi.Output[str]:
    return pulumi.Output.concat(
        "oci://",
        custom_registry_service.service_name,
        ".",
        custom_registry_service.namespace,
        ".svc.cluster.local:5000/charts",
    )


def _export_azure_config_outputs(init_stack_config: InitStackConfig) -> None:
    providers = init_stack_config.control_plane.infrastructure_providers
    azure_provider = providers.azure
    if azure_provider is None or not azure_provider.enabled:
        return
    if azure_provider.identity is None or azure_provider.default_subscription_id is None:
        return

    provider_names = [
        name
        for name, provider in (
            ("docker", providers.docker),
            ("azure", providers.azure),
        )
        if getattr(provider, "enabled", False)
    ]
    pulumi.export("capi_infrastructure_providers", provider_names)
    if azure_provider.identity.client_id is not None:
        pulumi.export("azure_client_ids", [str(azure_provider.identity.client_id)])
    if azure_provider.identity.tenant_id is not None:
        pulumi.export("azure_tenant_id", str(azure_provider.identity.tenant_id))
    pulumi.export("azure_host_subscription_id", str(azure_provider.default_subscription_id))
    if azure_provider.default_location is not None:
        pulumi.export("azure_host_location", azure_provider.default_location)
    if azure_provider.default_resource_group is not None:
        pulumi.export("azure_host_resource_group", azure_provider.default_resource_group)


def _azure_infrastructure_enabled(init_stack_config: InitStackConfig) -> bool:
    azure_provider = init_stack_config.control_plane.infrastructure_providers.azure
    return azure_provider is not None and azure_provider.enabled


def _export_common_outputs(
    *,
    cache_registry: CtlptlRegistry,
    cluster: CtlptlCluster,
    lb: CloudProviderKind,
    repo: GitOpsRepository,
    gitops_provider: str,
    gitops_webhook: GitOpsWebhook,
    pko: PKOBootstrap,
) -> None:
    pulumi.export("cache_registry_name", cache_registry.registry_name)
    pulumi.export("cache_registry_port", cache_registry.port)
    pulumi.export("cluster_name", cluster.cluster_name)
    pulumi.export("context", cluster.context)
    pulumi.export("kubeconfig", cluster.kubeconfig)
    pulumi.export("cloud_provider_kind_pid", lb.pid)
    pulumi.export("cloud_provider_kind_log", lb.log_path)
    pulumi.export("cloud_provider_kind_lb_port_mapping", lb.enable_lb_port_mapping)

    pulumi.export("gitops_provider", gitops_provider)
    pulumi.export("gitops_url", repo.url)
    pulumi.export("gitops_url_external", repo.url_external)
    pulumi.export("gitops_default_branch", repo.default_branch)
    pulumi.export(
        "gitops_ssh_private_key_secret_name",
        repo.ssh_private_key_secret_name,
    )
    pulumi.export(
        "gitops_ssh_private_key_secret_namespace",
        repo.ssh_private_key_secret_namespace,
    )

    pulumi.export("pko_namespace", pko.namespace)
    pulumi.export("pko_service_account", pko.service_account)
    pulumi.export("pko_flux_source_name", pko.flux_source_name)
    pulumi.export("pko_flux_source_namespace", pko.flux_source_namespace)
    pulumi.export("pko_flux_receiver_url", repo.flux_receiver_url)
    pulumi.export("gitops_flux_webhook_id", gitops_webhook.hook_id)
    pulumi.export("pko_init_stack", pko.init_stack)


