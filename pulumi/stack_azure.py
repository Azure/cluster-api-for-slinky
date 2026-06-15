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
The four config keys this module reads must be set on the ``azure``
stack first. **None of them are secrets** \u2014 they're just GUIDs::

    pulumi stack init azure
    pulumi config set ca4s-infra:azureClientId       <uami-client-id>
    pulumi config set ca4s-infra:azurePrincipalId    <uami-principal-id>
    pulumi config set ca4s-infra:azureTenantId       <entra-tenant-id>
    pulumi config set ca4s-infra:azureSubscriptionId <subscription-id>
    pulumi up -s azure

Grab the UAMI's ``clientId`` and ``principalId`` together with::

    az identity show -g <rg> -n <uami-name> \\
        --query '{client:clientId, principal:principalId}'

Optional config keys
--------------------
* ``ca4s-infra:azureClusterIdentityAllowedNamespaces`` \u2014 list of
  namespace names whose workload-cluster CRs may reference the
  AzureClusterIdentity. Unset (the default) emits the CR with
  ``spec.allowedNamespaces: {}`` (CAPZ idiom for \"all namespaces\"),
  which is the right default for multi-tenant Phase 2. Set this only
  to tighten the default.
* ``ca4s-infra:skip_imds_preflight`` \u2014 boolean. When ``true``, skip
  the host-side IMDS check that confirms the UAMI is actually
  attached. Useful for off-Azure ``pulumi preview`` and tests.

IMDS reachability prerequisite
------------------------------
This module runs a host-side preflight (unless
``skip_imds_preflight=true``) that hits
``http://169.254.169.254/metadata/identity/oauth2/token`` and
confirms the configured ``azureClientId`` resolves to a UAMI
attached to this VM. That catches the two common Phase 1 failure
modes (running off-Azure, mistyped clientId) at plan time.

The preflight only proves the *language host* can hit IMDS. For CAPZ
to actually mint tokens at workload-cluster reconcile time (Phase 2),
the CAPZ pod inside the ``kind`` mgmt cluster must also be able to
reach ``169.254.169.254``. On a standard kind-on-Azure-VM topology
this works out of the box because the host VM has the link-local
route and Docker SNATs container traffic through it. See
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

import pulumi
import pulumi_kubernetes as k8s

from ctlptl import CloudProviderKind, CtlptlCluster, CtlptlRegistry
from gitrepo import GitOpsRepository, GitOpsWebhook
from pko import PKOBootstrap
from pko._flux import FluxInfrastructure
from pko._release import PKO_NAMESPACE
from stacks.control_plane.azure import check_uami_attached
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

    # Azure UAMI identifiers. All four are non-sensitive GUIDs that
    # identify the UAMI, its Entra object representation, its tenant,
    # and its home subscription — plain ``config.require()`` is the
    # right verb (no ``--secret`` needed when setting them).
    #
    # ``azurePrincipalId`` is required even though Phase 1 doesn't
    # consume it: surfacing it as a stack output now lets Phase 2 ASO
    # ``RoleAssignment`` / ``FederatedIdentityCredential`` CRs reference
    # the UAMI by its Entra object ID without a config round-trip.
    azure_client_id = config.require("azureClientId")
    azure_principal_id = config.require("azurePrincipalId")
    azure_tenant_id = config.require("azureTenantId")
    azure_subscription_id = config.require("azureSubscriptionId")

    # Optional restriction on which namespaces' workload-cluster CRs may
    # reference the AzureClusterIdentity. Unset (the default) emits
    # ``spec.allowedNamespaces: {}`` on the CR — the CAPZ convention
    # for "all namespaces" — which is the right default for multi-tenant
    # Phase 2. Set this only to tighten the default.
    azure_allowed_namespaces = config.get_object(
        "azureClusterIdentityAllowedNamespaces"
    )

    # IMDS preflight: validate from the host that the configured UAMI
    # is actually attached before we let the resource graph commit to a
    # CR that CAPZ won't be able to use. Skip for off-Azure dev loops
    # via ``pulumi config set skip_imds_preflight true -s azure``.
    if not config.get_bool("skip_imds_preflight"):
        check_uami_attached(azure_client_id)

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
        config=build_control_plane_azure_child_config(
            client_id=azure_client_id,
            principal_id=azure_principal_id,
            tenant_id=azure_tenant_id,
            subscription_id=azure_subscription_id,
            allowed_namespaces=_normalize_allowed_namespaces(
                azure_allowed_namespaces
            ),
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
    # included so Phase 2 ASO consumers can read it from stack state.
    pulumi.export("azure_client_id", azure_client_id)
    pulumi.export("azure_principal_id", azure_principal_id)
    pulumi.export("azure_tenant_id", azure_tenant_id)
    pulumi.export("azure_subscription_id", azure_subscription_id)


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
