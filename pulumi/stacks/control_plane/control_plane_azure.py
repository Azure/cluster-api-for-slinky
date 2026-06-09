"""Per-env control-plane component for the ``azure`` env.

Mirrors :mod:`stacks.control_plane.control_plane_local` in structure but
with two deliberate differences:

* **No AWX.** AWX node-level integration is out of scope for the CAPZ
  learning sandbox \u2014 this control plane installs only what's needed
  to understand CAPZ + ASO + the AzureClusterIdentity wiring.
* **azure infrastructure provider.** :class:`capi.ClusterAPIOperator`
  is invoked with ``infrastructure_providers=(\"azure\",)`` instead of
  the default ``(\"docker\",)``. The CAPI Operator's azure
  ``InfrastructureProvider`` CR auto-installs both the CAPZ controller
  (in ``capz-system``) and the Azure Service Operator controller
  alongside it; see https://capz.sigs.k8s.io/topics/aso \u2014
  \"Beginning with CAPZ v1.11.0, ASO's control plane will be installed
  automatically by clusterctl in the capz-system namespace alongside
  CAPZ's control plane components.\"

After CAPZ + ASO converge, this module submits a single
``AzureClusterIdentity`` CR via :class:`azure.AzureClusterIdentity`.
The identity is **UserAssignedMSI**-flavored: it carries no client
secret, and CAPZ obtains tokens at reconcile time from Azure IMDS
(``169.254.169.254``) using the UAMI attached to the host VM the
management ``kind`` cluster runs on.

The three identifiers needed to populate the CR are passed by
:class:`pko._init_stack_azure.InitStackAzure` via the ``spec`` kwarg.
None of these are secrets \u2014 they're just GUIDs that identify the UAMI,
its tenant, and its home subscription.

The Pulumi resource graph relies on the
``pulumi.com/waitFor=condition=Ready`` annotation on the azure
``InfrastructureProvider`` CR (set by :class:`capi.ClusterAPIOperator`)
to gate the AzureClusterIdentity creation behind CRD installation.
Without that wait, the API server would reject the CR with a
\"no matches for kind AzureClusterIdentity\" error.

What Phase 1 does NOT include
-----------------------------
* No workload-cluster resources (``AzureCluster``,
  ``AzureManagedControlPlane``, ``MachinePool``, etc.) \u2014 the per-env
  tenants component (``TenantsAzure``) is intentionally empty.
* No Calico or any CNI install (no workload cluster to install it on).
* No autoscaling, no SSH/node customization, no AWX, no Slurm.

Each of those lands in a subsequent phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pulumi

try:
    from .azure import AzureClusterIdentity
    from .capi import ClusterAPIOperator
    from .certmanager import CertManager
except ImportError:
    from azure import AzureClusterIdentity
    from capi import ClusterAPIOperator
    from certmanager import CertManager


@dataclass(frozen=True)
class ControlPlaneAzureSpec:
    """UAMI identifiers required to build the Azure control plane.

    All three are non-sensitive GUIDs:

    * ``client_id``        \u2014 the UAMI's ``clientId`` (NOT the
      ``principalId``); the CAPZ controller passes this to IMDS to
      select which UAMI to mint a token for.
    * ``tenant_id``        \u2014 the Entra tenant the UAMI lives in.
    * ``subscription_id``  \u2014 the subscription the UAMI has role
      assignments on. Not read by Phase 1; surfaced here so missing
      values fail at plan time, not deep into Phase 2 when
      ``AzureManagedControlPlane.spec.subscriptionID`` first needs it.
    """

    client_id: str
    tenant_id: str
    subscription_id: str


# Config keys read out of ``childConfig.azure``. The outer
# stack_azure.py writes these via PKOBootstrap(config=...); the
# InitStackAzure component unpacks them and hands the typed spec to
# ControlPlaneAzure. Single-sourcing the spellings here keeps the two
# endpoints (writer + reader) in lockstep.
CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY = "azure"
_CONFIG_CLIENT_ID = "clientId"
_CONFIG_TENANT_ID = "tenantId"
_CONFIG_SUBSCRIPTION_ID = "subscriptionId"


def parse_control_plane_azure_spec(
    value: object | None,
) -> ControlPlaneAzureSpec:
    """Parse the ``childConfig.azure`` map into a typed spec.

    The init-stack passes ``childConfig`` through as a plain ``dict``.
    The outer stack writes it via ``PKOBootstrap(config=...)``, so the
    shape contract has two endpoints: the outer ``stack_azure.py``
    builds the dict using the same key constants this function reads.
    """
    if value is None:
        raise ValueError(
            "missing required Azure control-plane config under "
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r}; the outer "
            "stack_azure.py must pass PKOBootstrap(config={...}) with an "
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r} entry"
        )
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r} config must be an "
            f"object; got {type(value).__name__}"
        )

    fields: dict[str, str] = {}
    for config_key, field_name in (
        (_CONFIG_CLIENT_ID, "client_id"),
        (_CONFIG_TENANT_ID, "tenant_id"),
        (_CONFIG_SUBSCRIPTION_ID, "subscription_id"),
    ):
        field_value = value.get(config_key)
        if not isinstance(field_value, str) or not field_value:
            raise ValueError(
                f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}.{config_key} must "
                "be a non-empty string"
            )
        fields[field_name] = field_value

    return ControlPlaneAzureSpec(**fields)


def build_control_plane_azure_child_config(
    *,
    client_id: str,
    tenant_id: str,
    subscription_id: str,
) -> dict[str, object]:
    """Build the dict the outer stack passes via PKOBootstrap(config=...).

    Symmetric with :func:`parse_control_plane_azure_spec` so adding a
    field touches both sides at once. The shape is::

        {CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY: {clientId, tenantId, subscriptionId}}
    """
    return {
        CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY: {
            _CONFIG_CLIENT_ID: client_id,
            _CONFIG_TENANT_ID: tenant_id,
            _CONFIG_SUBSCRIPTION_ID: subscription_id,
        },
    }


class ControlPlaneAzure(pulumi.ComponentResource):
    """Build the Azure control-plane resource graph.

    Order and reasoning:

    1. :class:`certmanager.CertManager` \u2014 cert-manager + CRDs.
       Prerequisite for the CAPI Operator's webhooks.
    2. :class:`capi.ClusterAPIOperator` with
       ``infrastructure_providers=(\"azure\",)`` \u2014 installs CAPI core +
       kubeadm bootstrap + kubeadm control-plane + the azure
       infrastructure provider. The CAPI Operator's azure provider
       brings the CAPZ controller and ASO into ``capz-system``, plus
       all CAPZ + ASO CRDs (including ``AzureClusterIdentity``).
    3. :class:`azure.AzureClusterIdentity` \u2014 creates a
       ``UserAssignedMSI``-typed identity that future workload-cluster
       reconciliations will use to obtain Azure AD tokens via the host
       VM's IMDS endpoint. No backing Secret \u2014 the UAMI's credential
       material never leaves Azure.

    cert-manager and the CAPI Operator are independent and install in
    parallel (Pulumi's DAG resolves the actual ordering). The identity
    waits on the CAPI Operator becoming Ready so the
    AzureClusterIdentity CRD exists before submission.
    """

    cert_manager_namespace: pulumi.Output[str]
    capi_operator_namespace: pulumi.Output[str]
    capi_provider_version: pulumi.Output[str]
    capi_provider_namespaces: dict[str, pulumi.Output[str]]
    azure_cluster_identity_name: pulumi.Output[str]
    azure_cluster_identity_namespace: pulumi.Output[str]
    control_plane_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        spec: ControlPlaneAzureSpec,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ControlPlaneAzure",
            name,
            props={},
            opts=opts,
        )

        def child_options() -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(parent=self)

        cert_manager = CertManager("cert-manager", opts=child_options())
        capi = ClusterAPIOperator(
            "cluster-api",
            cert_manager=cert_manager,
            infrastructure_providers=("azure",),
            opts=child_options(),
        )

        # UAMI carries no client secret \u2014 the AzureClusterIdentity CR
        # references the UAMI by its clientID, and the CAPZ controller
        # fetches tokens from IMDS at reconcile time.
        azure_cluster_identity = AzureClusterIdentity(
            "cluster-identity",
            client_id=spec.client_id,
            tenant_id=spec.tenant_id,
            # Phase 1 has no workload clusters; "default" is the namespace
            # workload-cluster CRs would land in once Phase 2 begins.
            allowed_namespaces=["default"],
            opts=pulumi.ResourceOptions(parent=self, depends_on=[capi]),
        )

        self.cert_manager_namespace = cert_manager.namespace
        self.capi_operator_namespace = capi.namespace
        self.capi_provider_version = capi.provider_version
        self.capi_provider_namespaces = capi.provider_namespaces
        self.azure_cluster_identity_name = azure_cluster_identity.identity_name
        self.azure_cluster_identity_namespace = (
            azure_cluster_identity.identity_namespace
        )
        # subscription_id is parsed/validated but unused at this layer.
        # Reference it here so future Phase 2 AzureManagedControlPlane
        # work has a documented hook. The underscore-assign quiets
        # unused-var warnings without affecting behavior.
        _ = spec.subscription_id
        # Phase 1 is intentionally the foundation only. Flip to True
        # once workload-cluster components + a real ``pulumi up``
        # validation run have proved the AzureClusterIdentity works
        # against ARM.
        self.control_plane_ready = pulumi.Output.from_input(False)
        self.todo = pulumi.Output.from_input(
            "Phase 1 scaffold only \u2014 add workload-cluster Azure "
            "components (AzureManagedControlPlane + MachinePool + "
            "AzureManagedMachinePool) in Phase 2."
        )

        self.register_outputs(
            {
                "cert_manager_namespace": self.cert_manager_namespace,
                "capi_operator_namespace": self.capi_operator_namespace,
                "capi_provider_version": self.capi_provider_version,
                "capi_provider_namespaces": self.capi_provider_namespaces,
                "azure_cluster_identity_name": self.azure_cluster_identity_name,
                "azure_cluster_identity_namespace": (
                    self.azure_cluster_identity_namespace
                ),
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
