"""Outer stack body for Kind-backed management clusters."""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import CloudProviderKind, CtlptlCluster, CtlptlRegistry
from gitrepo import GitOpsRepository, GitOpsWebhook
from localenv import LocalEnvironment, discover_local_environment
from pko import PKOBootstrap
from pko._flux import FluxInfrastructure
from pko._release import PKO_NAMESPACE
from stacks.control_plane.control_plane_config import (
    build_control_plane_kind_azure_child_config,
)
from stacks.workload_cluster.registry_setting import (
    REGISTRY_CONFIG_KEY,
    local_port_registry_setting,
)
from stacks.workload_cluster.workload_cluster_class_aks import (
    build_aks_workload_cluster_child_config,
)


def run_stack() -> None:
    """Build the Kind management-cluster graph from config and discovery."""
    config = pulumi.Config()

    local_environment = discover_local_environment(
        azure_client_id_hint=config.get("azureClientId"),
        azure_principal_id_hint=config.get("azurePrincipalId"),
        azure_tenant_id_hint=config.get("azureTenantId"),
        azure_subscription_id_hint=config.get("azureSubscriptionId"),
        azure_location_hint=config.get("azureLocation"),
        azure_resource_group_hint=config.get("azureResourceGroup"),
    )
    for warning in local_environment.warnings:
        pulumi.log.warn(warning)

    child_config: dict[str, object] = {}
    azure_exports: dict[str, object] = {}

    if local_environment.azure is not None:
        child_config.update(
            _azure_child_config(config, local_environment, azure_exports)
        )
    elif _azure_workload_requested(config):
        raise ValueError(
            "Azure workload-cluster config requires an Azure managed identity "
            "capability. Run from an Azure VM with IMDS available."
        )

    registry = CtlptlRegistry("registry")
    child_config[REGISTRY_CONFIG_KEY] = local_port_registry_setting(registry.port)

    cluster = CtlptlCluster("mgmt", registry_name=registry.registry_name)
    mgmt_provider = k8s.Provider("mgmt-k8s", kubeconfig=cluster.kubeconfig)

    pko_namespace = k8s.core.v1.Namespace(
        "pko-ns",
        metadata={"name": PKO_NAMESPACE},
        opts=pulumi.ResourceOptions(provider=mgmt_provider),
    )
    lb = CloudProviderKind(
        "lb",
        enable_lb_port_mapping=config.get_bool("enable_lb_port_mapping", True),
    )
    flux = FluxInfrastructure("flux", provider=mgmt_provider)

    gitops_provider = config.get("gitops_provider") or "gitea-builtin"
    repo = GitOpsRepository(
        "gitops",
        gitops_provider_name=gitops_provider,
        gitops_provider_args={
            **(config.get_object("gitops_provider_args") or {}),
            "kubeconfig": cluster.kubeconfig,
            "flux_provider": mgmt_provider,
            "flux_infrastructure": flux,
            "pko_namespace_resource": pko_namespace,
            "sync_triggers": config.get_object("gitops_sync_triggers") or {},
        },
    )

    pko = PKOBootstrap(
        "pko",
        provider=mgmt_provider,
        namespace_resource=pko_namespace,
        flux_source_name=repo.flux_source_name,
        flux_source_resource=repo.flux_source,
        env=pulumi.get_stack(),
        config=child_config,
    )

    gitops_webhook = GitOpsWebhook(
        "gitops-flux-webhook",
        gitops_provider_name=gitops_provider,
        gitops_webhook_args=repo.webhook_args,
        opts=pulumi.ResourceOptions(depends_on=[pko]),
    )

    _export_common_outputs(
        registry=registry,
        cluster=cluster,
        lb=lb,
        repo=repo,
        gitops_provider=gitops_provider,
        gitops_webhook=gitops_webhook,
        pko=pko,
    )
    for name, value in azure_exports.items():
        pulumi.export(name, value)


def _azure_child_config(
    config: pulumi.Config,
    local_environment: LocalEnvironment,
    exports: dict[str, object],
) -> dict[str, object]:
    azure_environment = local_environment.azure
    if azure_environment is None:
        raise ValueError("Azure child config requires discovered Azure capability")

    azure_location = azure_environment.location
    azure_resource_group = azure_environment.resource_group

    exports.update(
        {
            "capi_infrastructure_providers": list(
                local_environment.management_defaults.infrastructure_providers
            ),
            "azure_client_id": azure_environment.client_id,
            "azure_principal_id": azure_environment.principal_id,
            "azure_tenant_id": azure_environment.tenant_id,
            "azure_subscription_id": azure_environment.subscription_id,
            "azure_location": azure_location,
            "azure_resource_group": azure_resource_group,
            "azure_host_subscription_id": azure_environment.host_subscription_id,
            "azure_host_location": azure_environment.host_location,
            "azure_host_resource_group": azure_environment.host_resource_group,
        }
    )

    return {
        **build_control_plane_kind_azure_child_config(
            client_id=azure_environment.client_id,
            principal_id=azure_environment.principal_id,
            tenant_id=azure_environment.tenant_id,
            subscription_id=azure_environment.subscription_id,
            allowed_namespaces=_normalize_allowed_namespaces(
                config.get_object("azureClusterIdentityAllowedNamespaces")
            ),
            infrastructure_providers=(
                local_environment.management_defaults.infrastructure_providers
            ),
            skip_in_cluster_preflight=config.get_bool(
                "skip_in_cluster_preflight", False
            ),
            capz_vmss_flex_image=config.get("capzVmssFlexImage"),
        ),
        **build_aks_workload_cluster_child_config(
            location=azure_location,
            resource_group=azure_resource_group,
            additional_tags=_normalize_additional_tags(
                config.get_object("azureAdditionalTags")
            ),
            aks_kubernetes_version=config.get("aksKubernetesVersion"),
            aks_node_sku=config.get("aksNodeSku"),
            aks_node_count=config.get_int("aksNodeCount"),
        ),
    }


def _azure_workload_requested(config: pulumi.Config) -> bool:
    return any(
        config.get(key) is not None
        for key in (
            "aksKubernetesVersion",
            "aksNodeSku",
            "azureClientId",
            "azureLocation",
            "azurePrincipalId",
            "azureResourceGroup",
            "azureSubscriptionId",
            "azureTenantId",
        )
    ) or any(
        config.get_object(key) is not None
        for key in (
            "azureAdditionalTags",
            "azureClusterIdentityAllowedNamespaces",
        )
    )


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
    pulumi.export("pko_flux_source_name", repo.flux_source_name)
    pulumi.export("pko_flux_receiver_url", repo.flux_receiver_url)
    pulumi.export("gitops_flux_webhook_id", gitops_webhook.hook_id)
    pulumi.export("pko_init_stack", pko.init_stack)


def _normalize_allowed_namespaces(value: object | None) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(
            "azureClusterIdentityAllowedNamespaces must be a list of "
            f"non-empty namespace names; got {value!r}"
        )
    return list(value)


def _normalize_additional_tags(value: object | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and key and isinstance(tag_value, str)
        for key, tag_value in value.items()
    ):
        raise ValueError(
            "azureAdditionalTags must be an object mapping non-empty string "
            f"tag keys to string values; got {value!r}"
        )
    return dict(value)
