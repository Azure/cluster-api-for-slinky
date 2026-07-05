"""Outer stack body for Kind-backed management clusters."""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import CloudProviderKind, CloudProviderKindConfig, CtlptlCluster, CtlptlRegistry
from gitrepo import GitOpsConfig, GitOpsRepository, GitOpsWebhook
from fluxcd import FluxInfrastructure
from pko import PKOBootstrap, PKO_NAMESPACE
from stacks.workload_cluster.registry_setting import (
    LocalPortRegistrySetting,
)
from stacks.workload_cluster.tenants import (
    WorkloadClusterConfig,
)
from stacks.init.init_stack import InitStackConfig
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig


def run_stack() -> None:
    """Build the Kind management-cluster graph from config and discovery."""
    config = pulumi.Config()
    cloud_provider_kind_config = CloudProviderKindConfig.model_validate(
        config.get_object("cloudProviderKind") or {}
    )
    gitops_config = GitOpsConfig.model_validate(config.get_object("gitops") or {})

    registry = CtlptlRegistry("registry")
    cluster = CtlptlCluster("mgmt", registry_name=registry.registry_name)
    mgmt_provider = k8s.Provider("mgmt-k8s", kubeconfig=cluster.kubeconfig)

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
        },
    )

    init_stack_config = _with_local_registry_config(
        InitStackConfig.model_validate(config.get_object("initStack") or {}),
        LocalPortRegistrySetting(port=registry.port),
    )

    pko = PKOBootstrap(
        "pko",
        provider=mgmt_provider,
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
) -> None:
    pulumi.export("registry_name", registry.registry_name)
    pulumi.export("registry_port", registry.port)
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


