"""AzureClusterIdentity component for secretless CAPZ identity flavors.

This component creates a **single** Kubernetes object that CAPZ needs in
order to know which Azure identity to use when reconciling workload
clusters: an ``AzureClusterIdentity`` CR.

Visually::

    +---------------------------+
    | AzureClusterIdentity CR   |       no Secret; token source
    |   spec.type=              |       depends on identity type
    |     UserAssignedMSI       |
    |     or WorkloadIdentity   |
    |   spec.tenantID           |
    |   spec.clientID           |
    +---------------------------+

Unlike the ServicePrincipal flavor, there is NO backing Kubernetes
Secret. For ``UserAssignedMSI``, the CAPZ controller pod calls IMDS and
asks for a token bound to ``clientID``. For ``WorkloadIdentity``, CAPZ
uses the management cluster's OIDC/federated-credential setup.

Why UserAssignedMSI remains the default
---------------------------------------
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
For ``UserAssignedMSI`` to actually work at reconcile time, the CAPZ
controller pod must be able to send HTTP to ``169.254.169.254``. The
traffic path on a standard kind-on-Azure-VM topology is:

* CAPZ pod → kind node container's CNI (kindnet/Calico) → kind node
  container's eth0 (Docker bridge interface).
* kind node container → Docker bridge (``docker0`` or per-network
  bridge) → host VM's network namespace.
* Host VM has a link-local route for ``169.254.169.254/32`` populated
  by the Azure VM agent at boot. The packet egresses on that route to
  the hypervisor-served IMDS endpoint. Source IP from IMDS's view is
  the host VM's interface IP (Docker SNATs container traffic by
  default on Linux), so IMDS accepts the request regardless of the
  caller's container origin.

Things that break this path (and the failure mode for each):

* **Off-Azure host** — ``169.254.169.254`` is unreachable.
    The Azure-capable config path's host-side IMDS discovery catches this at
    plan time and
    aborts with a clear message.
* **CNI that drops link-local egress** (e.g. some Cilium policies
  with strict default-deny) — the preflight passes (host-side path is
  fine) but the CAPZ pod fails to acquire tokens. Symptom: CAPZ
  controller logs ``ManagedIdentityCredential: ... no available
  identities``. Mitigation: add an explicit allow rule for
  ``169.254.169.254/32:80`` in your CNI policy, or fall back to
  ServicePrincipal identity.
* **kind nodes with Docker network isolation** (``--network none`` or
  a custom user-defined network without the default bridge) — the
  link-local route isn't reachable from inside the container.
  Mitigation: revert to the default kind networking, or patch the
  CAPZ Deployment to use ``hostNetwork: true`` so it shares the kind
  node container's network namespace directly.
* **Off-Azure mgmt cluster** (kind on a laptop, GKE, EKS) — same as
  above. Switch to ServicePrincipal (carries a clientSecret) or
  WorkloadIdentity (needs an OIDC issuer Entra can reach).

The host-side preflight in the Azure outer stack is a *necessary but not
sufficient* signal: if it passes, the kind nodes on the same host
typically also work, but a CNI install between Phase 1 and the first
workload-cluster reconcile can silently break the in-cluster path.
When Phase 2 lands, add an in-cluster preflight Job (curl IMDS from
inside ``capz-system``) to catch this drift.

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
``ControlPlaneKind``, *after* ``ClusterAPIOperator(...)`` has released
the CAPI Operator chart and the azure InfrastructureProvider CR has been
reconciled by the operator (the CR carries
``waitFor=condition=Ready`` so the Pulumi DAG blocks downstream
resources until then).
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from lib.config import PulumiConfigModel
from stacks.control_plane.control_plane_config import AzureClusterIdentityBaseConfig


# CAPZ v1.24.1 still serves AzureClusterIdentity on the v1beta1 API
# surface (the v1beta2 transition is in flight for some types but not
# for the identity CRDs). Pin explicitly so a CAPZ bump that lands a
# v1beta2 version doesn't silently switch us behind the scenes.
_AZURE_CLUSTER_IDENTITY_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta1"
_AZURE_CLUSTER_IDENTITY_KIND = "AzureClusterIdentity"


def _allowed_namespaces_spec(
    allowed_namespaces: Mapping[str, object] | PulumiConfigModel | None,
) -> dict[str, object]:
    if allowed_namespaces is None:
        return {}
    if isinstance(allowed_namespaces, PulumiConfigModel):
        return allowed_namespaces.to_config()
    return {
        key: value
        for key, value in dict(allowed_namespaces).items()
        if value is not None
    }


def _identity_spec(identity: AzureClusterIdentityBaseConfig) -> dict[str, object]:
    return {
        "type": identity.type,
        "tenantID": str(identity.tenant_id),
        "clientID": str(identity.client_id),
        "allowedNamespaces": _allowed_namespaces_spec(identity.allowed_namespaces),
    }


class AzureClusterIdentity(pulumi.ComponentResource):
    """``AzureClusterIdentity`` CR.

    Args:
        name:
            Pulumi resource name. Used as a prefix for the child CR
            only. The k8s object name is controlled by the
            ``identity_name`` parameter below so external references
            (e.g. an ``AzureCluster``'s ``spec.identityRef.name``) stay
            stable across Pulumi resource renames.
        identity:
            Parsed control-plane Azure identity config. The component
            renders the CAPZ ``spec.type``, ``spec.clientID``,
            ``spec.tenantID``, and ``spec.allowedNamespaces`` fields
            from this object. Confirm tenant ID with::

                az account show --query tenantId -o tsv

            ``identity.allowed_namespaces`` controls which namespaces'
            workload-cluster CRs may reference this identity. CAPZ
            admission treats the empty object as "any namespace may
            reference this identity". ``{"list": []}`` means "no
            namespace may reference this identity".
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
        identity: AzureClusterIdentityBaseConfig,
        namespace: str = "default",
        identity_name: str = "cluster-identity",
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:azure:AzureClusterIdentity", name, props={}, opts=opts
        )

        # CAPZ schema for ``AzureClusterIdentity.spec.allowedNamespaces``:
        #
        #   * ``{}``                    -> ALL namespaces
        #   * ``{list: [\"a\", \"b\"]}``   -> only \"a\", \"b\"
        #   * ``{list: []}``            -> no namespaces
        #
        # We default to the empty-struct \"allow all\" form so multi-tenant
        # Phase 2 (per-tenant namespaces) works without a Phase 1
        # restriction needing to be relaxed later. Callers can still pass
        # an explicit list or selector to tighten things.
        spec = _identity_spec(identity)

        # UserAssignedMSI and WorkloadIdentity both carry NO
        # ``spec.clientSecret`` field. The CR alone is sufficient once
        # the management-cluster credential path for the selected type
        # is configured outside this object.
        identity = k8s.apiextensions.CustomResource(
            f"{name}-cr",
            api_version=_AZURE_CLUSTER_IDENTITY_API_VERSION,
            kind=_AZURE_CLUSTER_IDENTITY_KIND,
            metadata={
                "name": identity_name,
                "namespace": namespace,
            },
            spec=spec,
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
        # resource graph -- Pulumi tracks it via parent=self.
        _ = identity

        self.register_outputs(
            {
                "identity_name": self.identity_name,
                "identity_namespace": self.identity_namespace,
            }
        )
