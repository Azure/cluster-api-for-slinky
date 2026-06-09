"""AzureClusterIdentity component (UserAssignedMSI flavor).

This component creates a **single** Kubernetes object that CAPZ needs in
order to know which Azure identity to use when reconciling workload
clusters: an ``AzureClusterIdentity`` CR of
``spec.type: UserAssignedMSI``.

Visually::

    +---------------------------+
    | AzureClusterIdentity CR   |       no Secret \u2014 IMDS provides
    |   spec.type=              |       tokens at runtime via:
    |     UserAssignedMSI       |       http://169.254.169.254/...
    |   spec.tenantID           |       ?client_id=<UAMI clientID>
    |   spec.clientID  (= UAMI) |
    +---------------------------+

Unlike the ServicePrincipal flavor, there is NO backing Kubernetes
Secret. The UAMI's credential material never lives in the cluster.
The CAPZ controller pod (in ``capz-system``) calls the Azure Instance
Metadata Service (IMDS) endpoint and asks for a token bound to the
UAMI identified by ``clientID``. IMDS responds with a short-lived
Azure AD token that CAPZ then uses to call ARM.

Why UserAssignedMSI was picked here
-----------------------------------
CAPZ supports four identity flavors. Compare:

==================  =================  ============================
Flavor              Backing Secret?    Works where?
==================  =================  ============================
ServicePrincipal    Yes (clientSecret) Anywhere (kind on laptop OK)
UserAssignedMSI     No                 Mgmt cluster MUST run on an
                                       Azure VM that has the UAMI
                                       attached
WorkloadIdentity    No                 Anywhere, but needs an OIDC
                                       issuer URL Entra can reach
                                       (requires JWKS hosting if kind)
==================  =================  ============================

The current project setup is:

* Management cluster runs as ``kind`` on an Azure Linux VM.
* That VM has a user-assigned managed identity (UAMI) attached
  (``az vm identity show`` confirms ``type: UserAssigned``).
* The UAMI has ``Contributor`` on the subscription where Phase 2
  will provision AKS clusters.

That makes ``UserAssignedMSI`` the right fit: no secret to rotate, no
JWKS to host, just the UAMI's clientID + tenantID in a CR.

Networking prerequisite: IMDS reachability
------------------------------------------
For this flavor to actually work at reconcile time, the CAPZ controller
pod must be able to send HTTP to ``169.254.169.254``. On a standard
kind-on-Azure-VM topology that works because:

* Pod \u2192 CNI \u2192 kind node container's network namespace.
* kind node container \u2192 Docker bridge \u2192 host VM's network.
* Host VM has the IMDS service at the link-local address served by the
  Azure hypervisor; requests originate from the VM's hypervisor identity
  regardless of which container made them.

If a future setup runs the mgmt cluster off-Azure (laptop, on-prem,
GKE), or installs a CNI that drops link-local traffic, this flavor will
silently fail at token-acquisition time. Switch to ServicePrincipal or
WorkloadIdentity in that case.

Resource ownership / reconciliation
-----------------------------------
The AzureClusterIdentity CR is a **Pulumi-managed resource**: Pulumi
creates it; the CAPZ controller does NOT mutate it. CAPZ only reads it
when reconciling ``AzureCluster``/``AzureManagedControlPlane`` resources
that reference the identity by ``spec.identityRef``.

The AzureClusterIdentity CRD must already be installed by the CAPZ
controller (via the CAPI Operator's ``InfrastructureProvider: azure``)
before this component runs. In the Phase 1 layout that ordering is
satisfied because this component is instantiated from
``ControlPlaneAzure``, *after* ``ClusterAPIOperator(...)`` has released
the CAPI Operator chart and the azure InfrastructureProvider CR has been
reconciled by the operator (the CR carries
``waitFor=condition=Ready`` so the Pulumi DAG blocks downstream
resources until then).
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions


# CAPZ v1.23.2 still serves AzureClusterIdentity on the v1beta1 API
# surface (the v1beta2 transition is in flight for some types but not
# for the identity CRDs). Pin explicitly so a CAPZ bump that lands a
# v1beta2 version doesn't silently switch us behind the scenes.
_AZURE_CLUSTER_IDENTITY_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta1"
_AZURE_CLUSTER_IDENTITY_KIND = "AzureClusterIdentity"


class AzureClusterIdentity(pulumi.ComponentResource):
    """``UserAssignedMSI``-flavored ``AzureClusterIdentity`` CR.

    Args:
        name:
            Pulumi resource name. Used as a prefix for the child CR
            only. The k8s object name is controlled by the
            ``identity_name`` parameter below so external references
            (e.g. an ``AzureCluster``'s ``spec.identityRef.name``) stay
            stable across Pulumi resource renames.
        client_id:
            ``clientId`` of the user-assigned managed identity (UAMI).
            NOT the ``principalId`` \u2014 those are two different GUIDs on
            the same identity. Confirm with::

                az identity show -g <rg> -n <uami-name> --query clientId
        tenant_id:
            Entra tenant ID the UAMI lives in. Same tenant as the
            subscription that owns the UAMI. Confirm with::

                az account show --query tenantId -o tsv
        allowed_namespaces:
            Namespaces whose workload-cluster CRs may reference this
            identity. CAPZ enforces this list at admission time. The
            default (``["default"]``) matches where Phase 1 will put
            workload-cluster CRs. Pass an empty list (``[]``) to allow
            ALL namespaces; ``None`` here means use the default.
        namespace:
            Namespace for the AzureClusterIdentity CR. There is no
            backing Secret to keep co-located with it for the UAMI
            flavor, but namespace placement still matters for the
            ``allowedNamespaces`` check on referencing workload-cluster
            CRs.
        identity_name:
            ``metadata.name`` of the AzureClusterIdentity CR. Future
            workload-cluster CRs reference this via
            ``spec.identityRef.name``. Defaults to ``cluster-identity``
            to match the upstream CAPZ documentation examples.
        provider:
            Kubernetes provider scoped to the management cluster. If
            None, Pulumi uses the ambient provider (suitable when
            running inside a PKO workspace pod against the in-cluster
            API).
        opts:
            Standard ``pulumi.ResourceOptions``.

    Outputs:
        identity_name:
            Output[str] echoing the AzureClusterIdentity ``metadata.name``.
            Suitable as the ``name`` field of an ``AzureCluster``'s
            ``spec.identityRef``.
        identity_namespace:
            Output[str] echoing the namespace.
    """

    identity_name: Output[str]
    identity_namespace: Output[str]

    def __init__(
        self,
        name: str,
        *,
        client_id: pulumi.Input[str],
        tenant_id: pulumi.Input[str],
        allowed_namespaces: list[str] | None = None,
        namespace: str = "default",
        identity_name: str = "cluster-identity",
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:azure:AzureClusterIdentity", name, props={}, opts=opts
        )

        if allowed_namespaces is None:
            allowed_namespaces = ["default"]

        # UserAssignedMSI carries NO ``spec.clientSecret`` field \u2014 CAPZ
        # obtains tokens at runtime from IMDS at 169.254.169.254 using
        # the UAMI selected by ``spec.clientID``. There is therefore no
        # backing Kubernetes Secret to create or wire up; the CR alone
        # is sufficient.
        identity = k8s.apiextensions.CustomResource(
            f"{name}-cr",
            api_version=_AZURE_CLUSTER_IDENTITY_API_VERSION,
            kind=_AZURE_CLUSTER_IDENTITY_KIND,
            metadata={
                "name": identity_name,
                "namespace": namespace,
            },
            spec={
                "type": "UserAssignedMSI",
                "tenantID": tenant_id,
                "clientID": client_id,
                "allowedNamespaces": {
                    "list": allowed_namespaces,
                },
            },
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # Outputs intentionally echo the inputs (rather than reading
        # back from .metadata) so they resolve at plan time, not just
        # after a successful create. Downstream resources need the name
        # string to build identity references, and waiting on a CR
        # readiness condition just to learn its own name is wasteful.
        self.identity_name = Output.from_input(identity_name)
        self.identity_namespace = Output.from_input(namespace)
        # Suppress unused-var warning while keeping the CR alive in the
        # resource graph \u2014 Pulumi tracks it via parent=self.
        _ = identity

        self.register_outputs(
            {
                "identity_name": self.identity_name,
                "identity_namespace": self.identity_namespace,
            }
        )
