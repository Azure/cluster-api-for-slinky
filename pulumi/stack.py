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
    CtlptlRegistry,
    CtlptlRegistryImage,
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
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig


_OWNER_TAG = "Owner"
_ADDITIONAL_IMAGES_CONFIG_KEY = "additionalImages"
_ADDITIONAL_IMAGES_REGISTRY_NAME = "additional-images-registry"
_ADDITIONAL_IMAGES_REGISTRY_ENV: list[pulumi.Input[str]] = [
    "CA4S_REGISTRY_MODE=additional-images",
]


class AdditionalImageConfig(PulumiConfigModel):
    source_path: NonEmptyStr
    source_ref: NonEmptyStr
    image_name: NonEmptyStr
    build_args: Mapping[NonEmptyStr, NonEmptyStr] | None = None


class AdditionalImagesConfig(PulumiConfigModel):
    registry_name: NonEmptyStr = _ADDITIONAL_IMAGES_REGISTRY_NAME
    registry_port: StrictPositiveInt | None = None
    images: Mapping[NonEmptyStr, AdditionalImageConfig] = {}


def run_stack() -> None:
    """Build the Kind management-cluster graph from config and discovery."""
    config = pulumi.Config()
    cloud_provider_kind_config = CloudProviderKindConfig.model_validate(
        config.get_object("cloudProviderKind") or {}
    )
    gitops_config = GitOpsConfig.model_validate(config.get_object("gitops") or {})
    additional_images_config_value = config.get_object(_ADDITIONAL_IMAGES_CONFIG_KEY)
    additional_images_config = (
        AdditionalImagesConfig.model_validate(additional_images_config_value)
        if additional_images_config_value is not None
        else None
    )

    registry = CtlptlRegistry("registry")
    additional_images_registry: CtlptlRegistry | None = None
    if additional_images_config is not None and additional_images_config.images:
        additional_images_registry = CtlptlRegistry(
            "additional-images-registry",
            registry_name=additional_images_config.registry_name,
            port=additional_images_config.registry_port,
            env=_ADDITIONAL_IMAGES_REGISTRY_ENV,
        )
    additional_images: dict[str, CtlptlRegistryImage] = {}
    if additional_images_config is not None and additional_images_registry is not None:
        for image_key, image_config in additional_images_config.images.items():
            build_args: dict[str, pulumi.Input[str]] | None = None
            if image_config.build_args is not None:
                build_args = dict(image_config.build_args)
            additional_images[image_key] = CtlptlRegistryImage(
                f"additional-image-{image_key}",
                source_path=image_config.source_path,
                source_ref=image_config.source_ref,
                registry_name=additional_images_registry.registry_name,
                registry_port=additional_images_registry.port,
                image_name=image_config.image_name,
                build_args=build_args,
                opts=pulumi.ResourceOptions(depends_on=[additional_images_registry]),
            )

    additional_registry_names: list[pulumi.Input[str]] | None = None
    if additional_images_registry is not None:
        additional_registry_names = [additional_images_registry.registry_name]
    cluster = CtlptlCluster(
        "mgmt",
        registry_name=registry.registry_name,
        additional_registry_names=additional_registry_names,
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

    init_stack_config = _with_owner_tag_config(
        _with_local_registry_config(
            InitStackConfig.model_validate(config.get_object("initStack") or {}),
            LocalPortRegistrySetting(port=registry.port),
        )
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
        registry=registry,
        cluster=cluster,
        lb=lb,
        repo=repo,
        gitops_provider=gitops_config.provider,
        gitops_webhook=gitops_webhook,
        pko=pko,
        additional_images=additional_images,
    )
    if _azure_infrastructure_enabled(init_stack_config):
        _export_azure_config_outputs(init_stack_config)


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
        if isinstance(workload_cluster, AKSWorkloadClusterConfig):
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
    registry: CtlptlRegistry,
    cluster: CtlptlCluster,
    lb: CloudProviderKind,
    repo: GitOpsRepository,
    gitops_provider: str,
    gitops_webhook: GitOpsWebhook,
    pko: PKOBootstrap,
    additional_images: Mapping[str, CtlptlRegistryImage],
) -> None:
    pulumi.export("registry_name", registry.registry_name)
    pulumi.export("registry_port", registry.port)
    if additional_images:
        pulumi.export(
            "additional_image_refs",
            {name: image.image_ref for name, image in additional_images.items()},
        )
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


