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
  provider that also auto-installs ASO) + a single
  ``AzureClusterIdentity`` CR of type ``UserAssignedMSI``.
* :class:`stacks.workload_cluster.tenants_azure.TenantsAzure` \u2014
  empty in Phase 1.

Phase 1 deliberate scope (and what's NOT here)
----------------------------------------------
* No workload-cluster CRs (``AzureCluster``, ``AzureManagedControlPlane``,
  ``MachinePool``, etc.). Those land when Phase 2 makes ``TenantsAzure``
  non-empty.
* No AWX, no Slurm, no autoscaling, no SSH/node customization.
* No ResourceGroup, VNet, or any other Azure-side resource via ASO. The
  azure infrastructure provider's controllers are installed but they
  reconcile nothing until Phase 2 introduces workload-cluster CRs that
  reference them.

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
The three config keys this module reads must be set on the ``azure``
stack first. **None of them are secrets** \u2014 they're just GUIDs::

    pulumi stack init azure
    pulumi config set ca4s-infra:azureClientId       <uami-client-id>
    pulumi config set ca4s-infra:azureTenantId       <entra-tenant-id>
    pulumi config set ca4s-infra:azureSubscriptionId <subscription-id>
    pulumi up -s azure

IMDS reachability prerequisite
------------------------------
For CAPZ to actually mint tokens at workload-cluster reconcile time
(Phase 2), the CAPZ pod inside the ``kind`` mgmt cluster must be able
to reach ``169.254.169.254``. On a standard kind-on-Azure-VM topology
this works out of the box. See
:mod:`stacks.control_plane.azure._cluster_identity` for the network
path walkthrough. If the kind CNI is ever replaced with one that
drops link-local egress, or this stack is ever run off-Azure, the
identity flavor will need to change to ServicePrincipal or
WorkloadIdentity.

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

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import CloudProviderKind, CtlptlCluster, CtlptlRegistry
from gitrepo import GitOpsRepository, GitOpsWebhook
from pko import PKOBootstrap
from pko._flux import FluxInfrastructure
from pko._release import PKO_NAMESPACE
from stacks.control_plane.control_plane_azure import (
    build_control_plane_azure_child_config,
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
    gitea_sync_triggers = config.get_object("gitea_sync_triggers") or {}
    configured_gitops_provider_args = config.get_object("gitops_provider_args") or {}

    # Azure UAMI identifiers. All three are non-sensitive GUIDs that
    # identify the UAMI, its tenant, and its home subscription \u2014
    # plain ``config.require()`` is the right verb (no ``--secret``
    # needed when setting them).
    azure_client_id = config.require("azureClientId")
    azure_tenant_id = config.require("azureTenantId")
    azure_subscription_id = config.require("azureSubscriptionId")

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
            "sync_triggers": gitea_sync_triggers,
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
        config=build_control_plane_azure_child_config(
            client_id=azure_client_id,
            tenant_id=azure_tenant_id,
            subscription_id=azure_subscription_id,
        ),
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
    pulumi.export("gitops_ssh_known_hosts", repo.ssh_known_hosts)
    pulumi.export(
        "gitops_ssh_private_key_secret_name",
        repo.ssh_private_key_secret.metadata["name"],
    )
    pulumi.export(
        "gitops_ssh_private_key_secret_namespace",
        repo.ssh_private_key_secret.metadata["namespace"],
    )

    pulumi.export("pko_namespace", pko.namespace)
    pulumi.export("pko_service_account", pko.service_account)
    pulumi.export("pko_flux_source_name", repo.flux_source_name)
    pulumi.export("pko_flux_receiver_url", repo.flux_receiver_url)
    pulumi.export("gitops_flux_webhook_id", gitops_webhook.hook_id)
    pulumi.export("pko_init_stack", pko.init_stack)

    # Echo non-secret Azure context so ``pulumi stack output`` confirms
    # the identifiers made it through to the resource graph. None are
    # secrets, so they're safe to surface verbatim.
    pulumi.export("azure_client_id", azure_client_id)
    pulumi.export("azure_tenant_id", azure_tenant_id)
    pulumi.export("azure_subscription_id", azure_subscription_id)
