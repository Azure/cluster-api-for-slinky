"""Stack body for the ``azure`` stack: kind + Gitea + Flux + PKO + CAPZ identity.

Mirror of :mod:`stack_local` for the Azure learning sandbox. This module
is imported by ``__main__.py`` when ``pulumi.get_stack() == \"azure\"``.

What this stack does (and doesn't) do
-------------------------------------
The outer ``pulumi up -s azure`` brings up, on this host:

1. A kind management cluster (via ``ctlptl``) with its own auto-named
   suffix so it coexists with any ``local``-stack kind cluster.
2. The in-cluster Gitea + Flux source plumbing the new architecture
   needs.
3. PKO + one ``ca4s-init`` Stack CR pointing at ``stacks/init/``.

PKO then runs the init stack inside the cluster, which dispatches to
:class:`pko._init_stack_azure.InitStackAzure`. That component
instantiates:

* :class:`stacks.control_plane.control_plane_azure.ControlPlaneAzure`
  \u2014 cert-manager + CAPI Operator (with the azure infrastructure
  provider) + a single
  ``AzureClusterIdentity`` CR of type ``UserAssignedMSI``.
* :class:`stacks.workload_cluster.tenants_azure.TenantsAzure` \u2014
  provisions a single AKS managed workload cluster (Phase 2).

Phase 2 scope (and what's NOT here yet)
---------------------------------------
* A single AKS *managed* cluster via the CAPZ managed CR set
  (``AzureManagedControlPlane`` / ``AzureManagedCluster`` /
  ``AzureManagedMachinePool`` + ``Cluster`` / ``MachinePool``). AKS owns
  its own CNI, storage, and bootstrap, so there is no Calico / kubeadm
  wiring on the workload side.
* No *self-managed* clusters yet (``AzureCluster`` +
  ``KubeadmControlPlane`` + ``AzureMachineTemplate``) — that is the next
  increment.
* No AWX, no Slurm, no autoscaling. No day-2 on the AKS cluster yet
  (workload kubeconfig retrieval + Slurm/Slinky install land later).
* The ResourceGroup is NOT created here — CAPZ provisions the AKS cluster
  into the operator-supplied ``azureResourceGroup`` (and AKS auto-creates
  the ``MC_*`` node resource group for the VMSS-backed nodes).

Identity model: UserAssignedMSI (no Secret)
-------------------------------------------
The host Linux VM must have a user-assigned managed identity (UAMI)
attached. The CR carries only ``tenantID`` + ``clientID``; the CAPZ
controller pod calls Azure IMDS (``169.254.169.254``) to mint tokens
at reconcile time. There is no Kubernetes Secret in the loop \u2014 the
three UAMI identifiers are non-sensitive GUIDs and flow as plain
Pulumi config values.

Confirm the host VM has a UAMI attached with::

    az vm identity show --resource-group <rg> --name <vm>
    # type: UserAssigned
    # userAssignedIdentities: { \"/subscriptions/.../<UAMI>\": {
    #     clientId: <UAMI-client-id>,
    #     principalId: <UAMI-principal-id>,
    # }}

Confirm the UAMI has the necessary role on the subscription (typically
``Contributor``)::

    az role assignment list --assignee <UAMI-client-id> --all -o table

One-time setup before ``pulumi up -s azure``
--------------------------------------------
When run on an Azure VM with a usable managed identity, this stack discovers
the identity, tenant, subscription, location, and host resource group from
IMDS. The old Azure keys remain as optional hints/overrides; none are secrets::

    pulumi stack init azure
    # Optional: select a specific managed identity instead of the IMDS default.
    # pulumi config set ca4s-infra:azureClientId    <mi-client-id>
    # pulumi config set ca4s-infra:azurePrincipalId <mi-principal-id>
    # pulumi config set ca4s-infra:azureTenantId    <entra-tenant-id>
    # Optional: override the IMDS-derived workload placement defaults.
    # pulumi config set ca4s-infra:azureSubscriptionId <subscription-id>
    # pulumi config set ca4s-infra:azureLocation       <region e.g. westus2>
    # pulumi config set ca4s-infra:azureResourceGroup  <existing-rg-name>
    pulumi up -s azure

Optional config keys
--------------------
* ``ca4s-infra:azureClientId`` — managed identity client ID hint. When set,
    host discovery asks IMDS for this identity first; if IMDS refuses it,
    discovery falls back to the default identity selected by IMDS and logs a
    warning.
* ``ca4s-infra:azurePrincipalId`` — managed identity principal/object ID hint.
    Normally discovered from the IMDS token's ``oid`` claim; this is a fallback
    for unusual token shapes and for future role-assignment consumers.
* ``ca4s-infra:azureTenantId`` — tenant ID hint. Normally discovered from the
    IMDS token's ``tid`` claim.
* ``ca4s-infra:azureSubscriptionId`` — workload subscription override. Unset
    uses the host VM subscription from IMDS.
* ``ca4s-infra:azureLocation`` — workload location override. Unset uses the
    host VM location from IMDS.
* ``ca4s-infra:azureResourceGroup`` — workload resource group override. Unset
    uses the host VM resource group from IMDS.
* ``ca4s-infra:aksKubernetesVersion`` — AKS control-plane Kubernetes
  version (e.g. ``v1.30.6``). Unset uses the default pinned in
  :mod:`stacks.workload_cluster.workload_cluster_azure_aks`. VERIFY the
  version is currently offered in ``azureLocation`` with
  ``az aks get-versions --location <region> -o table`` — AKS rejects
  unsupported versions and the ``waitFor=condition=Ready`` gate turns
  that into a hard ``pulumi up`` failure.
* ``ca4s-infra:aksNodeSku`` — VM SKU for the AKS system node pool (e.g.
  ``Standard_D2s_v3``). Unset uses the module default.
* ``ca4s-infra:aksNodeCount`` — integer node count for the system pool.
  Unset uses the module default (1).
* ``ca4s-infra:azureClusterIdentityAllowedNamespaces`` \u2014 list of
  namespace names whose workload-cluster CRs may reference the
  AzureClusterIdentity. Unset (the default) emits the CR with
  ``spec.allowedNamespaces: {}`` (CAPZ idiom for \"all namespaces\"),
  which is the right default for multi-tenant Phase 2. Set this only
  to tighten the default.
* ``ca4s-infra:skip_in_cluster_preflight`` \u2014 boolean. When ``true``,
  skip the in-cluster IMDS preflight Job that ``ControlPlaneAzure``
  schedules into ``capz-system`` after CAPZ is installed.

IMDS reachability prerequisite
------------------------------
This module always runs host-side discovery against
``http://169.254.169.254/metadata/instance`` and
``/metadata/identity/oauth2/token``. If a configured ``azureClientId`` hint
cannot mint a token, discovery falls back to the default managed identity IMDS
selects for this VM and logs a warning.

The host-side preflight only proves the *language host* can hit IMDS.
For the in-cluster path, ``ControlPlaneAzure`` schedules a one-shot
``IMDSPreflightJob`` into ``capz-system`` after CAPZ is installed; that
Job carries ``pulumi.com/waitFor=condition=Complete`` so the outer
``pulumi up`` blocks until the routing path **pod \u2192 kindnet \u2192 docker
bridge SNAT \u2192 host route \u2192 IMDS** has been empirically verified.

For CAPZ to actually mint tokens at workload-cluster reconcile time
(Phase 2), the CAPZ pod inside the ``kind`` mgmt cluster must also
be able to reach ``169.254.169.254``. On a standard kind-on-Azure-VM
topology this works out of the box because the host VM has the
link-local route and Docker SNATs container traffic through it. See
:mod:`stacks.control_plane.azure._cluster_identity` for the network
path walkthrough and the failure modes to watch for if the kind CNI
is ever replaced or this stack is ever run off-Azure.

Two-management-cluster coexistence
----------------------------------
Running ``pulumi up -s azure`` in parallel with ``pulumi up -s local``
creates a *second* kind cluster on the same host. The two are isolated
by ctlptl's per-stack auto-named ``kind-mgmt-<hex>`` suffix and by
Pulumi's per-stack state. They share the singleton
``cloud-provider-kind`` daemon (which polls Docker for any kind
cluster regardless of stack) and the host Docker network. If running
both is tight on your host, ``pulumi destroy -s local`` first.
"""

from __future__ import annotations

from typing import Mapping

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import CloudProviderKind, CtlptlCluster, CtlptlRegistry
from gitrepo import GitOpsRepository, GitOpsWebhook
from localenv import discover_local_environment
from pko import PKOBootstrap
from pko._flux import FluxInfrastructure
from pko._release import PKO_NAMESPACE
from stacks.control_plane.control_plane_azure import (
    build_control_plane_azure_child_config,
)
from stacks.workload_cluster.workload_cluster_azure_aks import (
    build_azure_workload_child_config,
)


def run() -> None:
    """Build the ``azure`` stack's resource graph and export its outputs.

    Same shape as :mod:`stack_local`:

      Phase 1 \u2014 kind cluster + image registry + LB controller
      Phase 2 \u2014 GitOps source (Gitea by default) + Flux
      Phase 3 \u2014 PKO bootstrap + the single ``ca4s-init`` Stack CR
                 carrying the UAMI identifiers in ``spec.config``
      Phase 4 \u2014 stack outputs

    The materially different bit vs ``stack_local`` is Phase 3: we
    pass ``PKOBootstrap(config={...})`` with the UAMI identifiers so
    the init stack can pass them through to ``ControlPlaneAzure``.
    """
    config = pulumi.Config()

    # Same default as stack_local: enable host port-mapping for
    # cloud-provider-kind. Override with ``pulumi config set
    # enable_lb_port_mapping false -s azure`` on pure-Linux hosts.
    enable_lb_port_mapping = config.get_bool("enable_lb_port_mapping")
    if enable_lb_port_mapping is None:
        enable_lb_port_mapping = True

    gitops_provider = config.get("gitops_provider") or "gitea-builtin"
    # Operator-controlled replacement inputs for the one-shot Git sync.
    # Example:
    #     pulumi config set --path 'gitops_sync_triggers.generation' rerun-1 -s azure
    # Bump any key/value to force a normal non-force push without changing
    # HEAD. ``gitea_sync_triggers`` accepted for backwards compatibility with
    # operators that still set the old key.
    gitops_sync_triggers = (
        config.get_object("gitops_sync_triggers")
        or config.get_object("gitea_sync_triggers")
        or {}
    )
    configured_gitops_provider_args = config.get_object("gitops_provider_args") or {}

    # Discover the local host's Azure capability before declaring resources.
    # Config values are hints/overrides: IMDS supplies identity, subscription,
    # location, and resource group on the Azure VM that hosts this kind stack.
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

    if local_environment.azure is None:
        raise ValueError(
            "azure stack requires an Azure managed identity capability. Run "
            "from an Azure VM with IMDS available."
        )
    azure_environment = local_environment.azure

    # Optional restriction on which namespaces' workload-cluster CRs may
    # reference the AzureClusterIdentity. Unset (the default) emits
    # ``spec.allowedNamespaces: {}`` on the CR — the CAPZ convention
    # for "all namespaces" — which is the right default for multi-tenant
    # Phase 2. Set this only to tighten the default.
    azure_allowed_namespaces = config.get_object(
        "azureClusterIdentityAllowedNamespaces"
    )

    # Workload-cluster placement + sizing for the AKS managed cluster that
    # TenantsAzure provisions. location + resource group default to the host
    # VM's IMDS metadata, with config still accepted as an explicit override.
    # The AKS sizing keys are optional and fall back to the defaults baked into
    # workload_cluster_azure_aks when unset.
    azure_location = azure_environment.location
    azure_resource_group = azure_environment.resource_group
    aks_kubernetes_version = config.get("aksKubernetesVersion")
    aks_node_sku = config.get("aksNodeSku")
    aks_node_count = config.get_int("aksNodeCount")

    # Extra Azure tags stamped onto EACH AKS agent pool's VMSS (via
    # AzureManagedMachinePool.spec.additionalTags). Required in this
    # environment to satisfy the org Azure Policy that demands an ``Owner``
    # tag on the VMs — AKS does not reliably propagate cluster-level tags
    # to the node VMSS, so the tag is set per machine pool. Set with::
    #
    #     pulumi config set --path 'azureAdditionalTags.Owner' <alias> -s azure
    #
    # Unset (the default) emits no ``additionalTags`` on the agent pool.
    azure_additional_tags = _normalize_additional_tags(
        config.get_object("azureAdditionalTags")
    )

    # Skip the in-cluster IMDS preflight Job that ``ControlPlaneAzure``
    # would otherwise schedule into ``capz-system``. Defaults to False so
    # production paths always get both layers of verification.
    skip_in_cluster_preflight = bool(
        config.get_bool("skip_in_cluster_preflight")
    )

    # ----------------------------------------------------------------------
    # Phase 1 \u2014 cluster + registry + LB controller. Identical shape to
    # stack_local; ctlptl's per-stack autonamed kind cluster name
    # differentiates this from any concurrently-running local cluster.
    # ----------------------------------------------------------------------
    registry = CtlptlRegistry("registry")

    cluster = CtlptlCluster(
        "mgmt",
        registry_name=registry.registry_name,
    )

    mgmt_provider = k8s.Provider(
        "mgmt-k8s",
        kubeconfig=cluster.kubeconfig,
    )

    pko_namespace = k8s.core.v1.Namespace(
        "pko-ns",
        metadata={"name": PKO_NAMESPACE},
        opts=pulumi.ResourceOptions(
            provider=mgmt_provider,
        ),
    )

    lb = CloudProviderKind("lb", enable_lb_port_mapping=enable_lb_port_mapping)

    flux = FluxInfrastructure(
        "flux",
        provider=mgmt_provider,
    )

    # ----------------------------------------------------------------------
    # Phase 2 \u2014 GitOps source. Same as stack_local.
    # ----------------------------------------------------------------------
    repo = GitOpsRepository(
        "gitops",
        gitops_provider_name=gitops_provider,
        gitops_provider_args={
            **configured_gitops_provider_args,
            "kubeconfig": cluster.kubeconfig,
            "flux_provider": mgmt_provider,
            "flux_infrastructure": flux,
            "pko_namespace_resource": pko_namespace,
            "sync_triggers": gitops_sync_triggers,
        },
    )

    # ----------------------------------------------------------------------
    # Phase 3 \u2014 PKO bootstrap + Azure UAMI identifier projection.
    #
    # We need the init stack to know which UAMI to put in the
    # AzureClusterIdentity CR. The new architecture provides exactly
    # the mechanism: PKOBootstrap(config=...) flows through the
    # init Stack CR's spec.config and lands in the workspace pod's
    # Pulumi config as ``ca4s-init:childConfig``. InitStackAzure
    # reads that map and unpacks ``childConfig.azure.{clientId,
    # tenantId, subscriptionId}`` into a typed spec for
    # ControlPlaneAzure.
    #
    # The shape of the dict is built by build_control_plane_azure_child_config
    # so the writer (here) and the reader (parse_control_plane_azure_spec
    # inside the init stack) stay in lockstep.
    # ----------------------------------------------------------------------
    pko = PKOBootstrap(
        "pko",
        provider=mgmt_provider,
        namespace_resource=pko_namespace,
        flux_source_name=repo.flux_source_name,
        flux_source_resource=repo.flux_source,
        env=pulumi.get_stack(),
        config={
            **build_control_plane_azure_child_config(
                client_id=azure_environment.client_id,
                principal_id=azure_environment.principal_id,
                tenant_id=azure_environment.tenant_id,
                subscription_id=azure_environment.subscription_id,
                allowed_namespaces=_normalize_allowed_namespaces(
                    azure_allowed_namespaces
                ),
                infrastructure_providers=(
                    local_environment.management_defaults.infrastructure_providers
                ),
                skip_in_cluster_preflight=skip_in_cluster_preflight,
            ),
            **build_azure_workload_child_config(
                location=azure_location,
                resource_group=azure_resource_group,
                additional_tags=azure_additional_tags,
                aks_kubernetes_version=aks_kubernetes_version,
                aks_node_sku=aks_node_sku,
                aks_node_count=aks_node_count,
            ),
        },
    )

    gitops_webhook = GitOpsWebhook(
        "gitops-flux-webhook",
        gitops_provider_name=gitops_provider,
        gitops_webhook_args=repo.webhook_args,
        opts=pulumi.ResourceOptions(depends_on=[pko]),
    )

    # ----------------------------------------------------------------------
    # Phase 4 \u2014 stack outputs (same shape as stack_local, plus Azure echo).
    # ----------------------------------------------------------------------
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

    # Echo non-secret Azure context so ``pulumi stack output`` confirms
    # the identifiers made it through to the resource graph. None are
    # secrets, so they're safe to surface verbatim. principalId is
    # included so a later increment can read it from stack state.
    pulumi.export("capi_infrastructure_providers", list(
        local_environment.management_defaults.infrastructure_providers
    ))
    pulumi.export("azure_client_id", azure_environment.client_id)
    pulumi.export("azure_principal_id", azure_environment.principal_id)
    pulumi.export("azure_tenant_id", azure_environment.tenant_id)
    pulumi.export("azure_subscription_id", azure_environment.subscription_id)
    pulumi.export("azure_location", azure_location)
    pulumi.export("azure_resource_group", azure_resource_group)
    pulumi.export("azure_host_subscription_id", azure_environment.host_subscription_id)
    pulumi.export("azure_host_location", azure_environment.host_location)
    pulumi.export("azure_host_resource_group", azure_environment.host_resource_group)


def _normalize_allowed_namespaces(
    value: object | None,
) -> list[str] | None:
    """Coerce the ``azureClusterIdentityAllowedNamespaces`` config value.

    Pulumi's ``get_object`` returns the parsed JSON value verbatim. We
    accept ``None`` (key not set) and lists of strings. Any other shape
    is a config typo — fail at plan time with a clear message instead
    of letting it land in the resource graph as garbage.
    """
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


def _normalize_additional_tags(
    value: object | None,
) -> dict[str, str] | None:
    """Coerce the ``azureAdditionalTags`` config value into a tag map.

    Pulumi's ``get_object`` returns the parsed JSON value verbatim. We accept
    ``None`` (key not set) and objects mapping string tag keys to string
    values. Any other shape is a config typo — fail at plan time with a clear
    message instead of letting it land in the resource graph as garbage.
    """
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
