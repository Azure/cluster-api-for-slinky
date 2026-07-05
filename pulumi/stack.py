"""Outer stack body for Kind-backed management clusters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import CloudProviderKind, CloudProviderKindConfig, CtlptlCluster, CtlptlRegistry
from gitrepo import GitOpsConfig, GitOpsRepository, GitOpsWebhook
from localenv import LocalEnvironment, discover_local_environment
from fluxcd import FluxInfrastructure
from pko import PKOBootstrap, PKO_NAMESPACE
from stacks.control_plane.control_plane_config import (
    AzureInfrastructureProviderConfig,
    ControlPlaneAWXConfig,
    ControlPlaneDeploymentsConfig,
    ControlPlaneKindConfig,
    InfrastructureProvidersConfig,
)
from stacks.workload_cluster.registry_setting import (
    LocalPortRegistrySetting,
)
from stacks.workload_cluster.tenants import (
    WorkloadClusterConfig,
    TenantsConfig,
)
from stacks.init.init_stack import InitStackConfig
from stacks.workload_cluster.workload_cluster_class_aks import (
    AKSWorkloadClusterConfig,
    AzureWorkloadSpec,
)
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig


class _AzureIdentityConfig(Protocol):
    client_id: object
    tenant_id: object


class _AzureProviderConfig(Protocol):
    default_subscription_id: object | None
    identity: _AzureIdentityConfig | None


def run_stack() -> None:
    """Build the Kind management-cluster graph from config and discovery."""
    config = pulumi.Config()
    cloud_provider_kind_config = CloudProviderKindConfig.model_validate(
        config.get_object("cloudProviderKind") or {}
    )
    gitops_config = GitOpsConfig.model_validate(config.get_object("gitops") or {})

    local_environment = discover_local_environment()
    for warning in local_environment.warnings:
        pulumi.log.warn(warning)

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

    init_stack_config = InitStackConfig()
    if local_environment.azure is not None:
        init_stack_config = _azure_init_stack_config(
            config,
            local_environment,
        )
    elif _azure_workload_requested(config):
        raise ValueError(
            "Azure workload-cluster config requires an Azure managed identity "
            "capability. Run from an Azure VM with IMDS available."
        )
    init_stack_config = _with_local_registry_config(
        init_stack_config,
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
    if init_stack_config.control_plane.infrastructure_providers.azure is not None:
        _export_azure_config_outputs(init_stack_config)


def _azure_init_stack_config(
    config: pulumi.Config,
    local_environment: LocalEnvironment,
) -> InitStackConfig:
    azure_environment = local_environment.azure
    if azure_environment is None:
        raise ValueError("Azure child config requires discovered Azure capability")

    azure_config = config.require_object("azure")
    if not isinstance(azure_config, Mapping):
        raise ValueError("azure config must be an object")
    workload_parameters = AzureWorkloadSpec.model_validate(
        {
            **azure_config,
            "location": azure_environment.host_location,
            "resourceGroup": azure_environment.host_resource_group,
        }
    )

    control_plane_config = ControlPlaneKindConfig(
        infrastructure_providers=InfrastructureProvidersConfig().apply_local_environment_discovery(
            infrastructure_providers=(
                local_environment.management_defaults.infrastructure_providers
            ),
            azure_environment=local_environment.azure,
        ),
        deployments=ControlPlaneDeploymentsConfig(
            awx=ControlPlaneAWXConfig(enabled=False)
        ),
    )

    return InitStackConfig(
        control_plane=control_plane_config,
        tenants=TenantsConfig(
            workload_clusters={
                "caps-aks": AKSWorkloadClusterConfig(
                    parameters=workload_parameters,
                ),
            },
        ),
    )


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
    if not isinstance(azure_provider, AzureInfrastructureProviderConfig):
        return
    azure_config = cast(_AzureProviderConfig, azure_provider)
    if azure_config.identity is None or azure_config.default_subscription_id is None:
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
    pulumi.export("azure_client_ids", [str(azure_config.identity.client_id)])
    pulumi.export("azure_tenant_id", str(azure_config.identity.tenant_id))
    pulumi.export("azure_host_subscription_id", str(azure_config.default_subscription_id))

    for workload_cluster in init_stack_config.tenants.workload_clusters.values():
        if isinstance(workload_cluster, AKSWorkloadClusterConfig):
            pulumi.export("azure_host_location", workload_cluster.parameters.location)
            pulumi.export(
                "azure_host_resource_group",
                workload_cluster.parameters.resource_group,
            )
            break


def _azure_workload_requested(config: pulumi.Config) -> bool:
    return config.get_object("azure") is not None


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


