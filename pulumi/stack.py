"""Outer stack body for Kind-backed management clusters."""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import (
    CloudProviderKind,
    CloudProviderKindConfig,
    CtlptlCluster,
    CtlptlCustomRegistryImage,
    CtlptlCustomRegistryOCIArtifact,
    CtlptlRegistry,
    CtlptlRegistryService,
)
from gitrepo import GitOpsConfig, GitOpsRepository, GitOpsWebhook
from fluxcd import FluxInfrastructure
from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from pko import PKOBootstrap, PKO_NAMESPACE
from stacks.workload_cluster.registry_setting import (
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


_OWNER_TAG = "Owner"
_CUSTOM_IMAGES_CONFIG_KEY = "customImages"
_CAPZ_ARTIFACT_CONFIG_KEY = "capzArtifact"
_CUSTOM_REGISTRY_NAME = "custom-registry"
_CUSTOM_REGISTRY_ENV: list[pulumi.Input[str]] = [
    "CA4S_REGISTRY_MODE=custom-registry",
]
_CAPZ_ARTIFACT_NAME = "capz/cluster-api-provider-azure"
_CAPZ_CONTROLLER_IMAGE_KEY = "capz-controller"


class CustomImageConfig(PulumiConfigModel):
    source_path: NonEmptyStr
    source_ref: NonEmptyStr
    image_name: NonEmptyStr
    build_args: Mapping[NonEmptyStr, NonEmptyStr] | None = None


class CustomImagesConfig(PulumiConfigModel):
    registry_name: NonEmptyStr = _CUSTOM_REGISTRY_NAME
    registry_port: StrictPositiveInt | None = None
    images: Mapping[NonEmptyStr, CustomImageConfig] = {}


class CAPZArtifactConfig(PulumiConfigModel):
    source_path: NonEmptyStr
    source_ref: NonEmptyStr
    artifact_name: NonEmptyStr = _CAPZ_ARTIFACT_NAME


def run_stack() -> None:
    """Build the Kind management-cluster graph from config and discovery."""
    config = pulumi.Config()
    cloud_provider_kind_config = CloudProviderKindConfig.model_validate(
        config.get_object("cloudProviderKind") or {}
    )
    gitops_config = GitOpsConfig.model_validate(config.get_object("gitops") or {})
    custom_images_config_value = config.get_object(_CUSTOM_IMAGES_CONFIG_KEY)
    custom_images_config = (
        CustomImagesConfig.model_validate(custom_images_config_value)
        if custom_images_config_value is not None
        else None
    )
    capz_artifact_config_value = config.get_object(_CAPZ_ARTIFACT_CONFIG_KEY)
    capz_artifact_config = (
        CAPZArtifactConfig.model_validate(capz_artifact_config_value)
        if capz_artifact_config_value is not None
        else None
    )

    cache_registry = CtlptlRegistry("cache-registry")
    custom_registry: CtlptlRegistry | None = None
    if (
        custom_images_config is not None and custom_images_config.images
    ) or capz_artifact_config is not None:
        custom_registry = CtlptlRegistry(
            "custom-registry",
            registry_name=(
                custom_images_config.registry_name
                if custom_images_config is not None
                else _CUSTOM_REGISTRY_NAME
            ),
            port=(
                custom_images_config.registry_port
                if custom_images_config is not None
                else None
            ),
            env=_CUSTOM_REGISTRY_ENV,
        )
    custom_images: dict[str, CtlptlCustomRegistryImage] = {}
    if custom_images_config is not None and custom_registry is not None:
        for image_key, image_config in custom_images_config.images.items():
            build_args: dict[str, pulumi.Input[str]] | None = None
            if image_config.build_args is not None:
                build_args = dict(image_config.build_args)
            custom_images[image_key] = CtlptlCustomRegistryImage(
                f"custom-image-{image_key}",
                source_path=image_config.source_path,
                source_ref=image_config.source_ref,
                registry_name=custom_registry.registry_name,
                registry_port=custom_registry.port,
                image_name=image_config.image_name,
                build_args=build_args,
                opts=pulumi.ResourceOptions(depends_on=[custom_registry]),
            )
    capz_artifact: CtlptlCustomRegistryOCIArtifact | None = None
    if capz_artifact_config is not None and custom_registry is not None:
        capz_artifact = CtlptlCustomRegistryOCIArtifact(
            "capz-artifact",
            source_path=capz_artifact_config.source_path,
            source_ref=capz_artifact_config.source_ref,
            registry_name=custom_registry.registry_name,
            registry_port=custom_registry.port,
            artifact_name=capz_artifact_config.artifact_name,
            opts=pulumi.ResourceOptions(depends_on=[custom_registry]),
        )

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

    base_init_stack_config = _with_owner_tag_config(
        _with_local_registry_config(
            InitStackConfig.model_validate(config.get_object("initStack") or {}),
            LocalPortRegistrySetting(port=cache_registry.port),
        )
    )
    capz_controller_image_ref: pulumi.Output[str] | None = None
    if _CAPZ_CONTROLLER_IMAGE_KEY in custom_images:
        capz_controller_image_ref = custom_images[_CAPZ_CONTROLLER_IMAGE_KEY].image_ref
    init_stack_config = _with_capz_provider_overrides(
        base_init_stack_config,
        provider_oci=capz_artifact_oci_url,
        controller_image=capz_controller_image_ref,
    )

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
        custom_images=custom_images,
        capz_artifact=capz_artifact,
        capz_artifact_oci_url=capz_artifact_oci_url,
        custom_registry_service=custom_registry_service,
    )
    if _azure_infrastructure_enabled(base_init_stack_config):
        _export_azure_config_outputs(base_init_stack_config)


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
    capz_artifact: CtlptlCustomRegistryOCIArtifact,
    custom_registry_service: CtlptlRegistryService,
) -> pulumi.Output[str]:
    return pulumi.Output.concat(
        custom_registry_service.url,
        "/",
        capz_artifact.artifact_name,
        ":",
        capz_artifact.artifact_tag,
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
    custom_images: Mapping[str, CtlptlCustomRegistryImage],
    capz_artifact: CtlptlCustomRegistryOCIArtifact | None,
    capz_artifact_oci_url: pulumi.Output[str] | None,
    custom_registry_service: CtlptlRegistryService | None,
) -> None:
    pulumi.export("cache_registry_name", cache_registry.registry_name)
    pulumi.export("cache_registry_port", cache_registry.port)
    if custom_images:
        pulumi.export(
            "custom_image_refs",
            {name: image.image_ref for name, image in custom_images.items()},
        )
    if capz_artifact is not None:
        pulumi.export("capz_artifact_ref", capz_artifact.artifact_ref)
        if capz_artifact_oci_url is not None:
            pulumi.export("capz_artifact_oci_url", capz_artifact_oci_url)
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


