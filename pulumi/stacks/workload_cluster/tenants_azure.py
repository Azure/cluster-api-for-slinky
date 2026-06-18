"""Tenants aggregate for the ``azure`` outer env.

Phase 2 of the CAPZ migration. Where Phase 1 left this component an
intentional no-op (identity foundation only), it now provisions a **single
AKS managed workload cluster** via
:class:`stacks.workload_cluster.workload_cluster_azure_aks.WorkloadClusterAzureAKS`.

:class:`pko._init_stack_azure.InitStackAzure` instantiates this alongside
``ControlPlaneAzure`` and threads in (a) the parsed
:class:`~stacks.workload_cluster.workload_cluster_azure_aks.AzureWorkloadSpec`
describing where/how big the cluster should be, (b) the subscription ID from
the Phase 1 identity spec, and (c) the ``AzureClusterIdentity`` name +
namespace outputs so the AKS control plane's ``identityRef`` points back at
the Phase 1 identity.

This is the Azure analogue of :mod:`stacks.workload_cluster.tenants_local`,
but deliberately simpler: there is a single hardcoded AKS tenant rather than
a config-driven fan-out. A multi-tenant fan-out (and a self-managed cluster
class) can be added later by mirroring ``tenants_local``'s importlib
dispatch; for now a single managed cluster keeps the first working CAPZ
workload increment small.
"""

from __future__ import annotations

import pulumi
from pulumi import ResourceOptions

try:
    from .workload_cluster_azure_aks import (
        AzureWorkloadSpec,
        WorkloadClusterAzureAKS,
    )
except ImportError:  # pragma: no cover - exercised only outside the package
    from workload_cluster_azure_aks import (
        AzureWorkloadSpec,
        WorkloadClusterAzureAKS,
    )


# Single hardcoded tenant for this increment. Used as the CR/AKS cluster
# name prefix (``caps-aks``, ``caps-aks-pool0``).
_AKS_INSTANCE_NAME = "caps-aks"


class TenantsAzure(pulumi.ComponentResource):
    """Instantiate the ``azure`` workload-cluster tenant(s).

    Args:
        name: Pulumi resource name.
        workload_spec: parsed workload-cluster placement/sizing spec.
        subscription_id: Azure subscription ID (from the Phase 1 identity
            spec; the AKS control plane is billed to it).
        identity_name / identity_namespace: name + namespace of the
            ``AzureClusterIdentity`` created by ``ControlPlaneAzure``,
            referenced by the AKS control plane's ``identityRef``.
        opts: standard ``pulumi.ResourceOptions``.

    Outputs:
        workload_clusters: one-element list describing the AKS cluster. The
            list shape (not a bare dict) is preserved so the init stack and
            outer ``stack_azure.py`` consume it the same way as
            :class:`stacks.workload_cluster.tenants_local.TenantsLocal`.
    """

    workload_clusters: list[dict[str, object]]

    def __init__(
        self,
        name: str,
        *,
        workload_spec: AzureWorkloadSpec,
        subscription_id: str,
        identity_name: pulumi.Input[str],
        identity_namespace: pulumi.Input[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:TenantsAzure",
            name,
            props={},
            opts=opts,
        )

        aks = WorkloadClusterAzureAKS(
            _AKS_INSTANCE_NAME,
            instance=_AKS_INSTANCE_NAME,
            subscription_id=subscription_id,
            identity_name=identity_name,
            identity_namespace=identity_namespace,
            location=workload_spec.location,
            resource_group=workload_spec.resource_group,
            kubernetes_version=workload_spec.kubernetes_version,
            node_sku=workload_spec.node_sku,
            node_count=workload_spec.node_count,
            additional_tags=workload_spec.additional_tags,
            opts=ResourceOptions(parent=self),
        )

        # List shape (not a bare dict) mirrors ``TenantsLocal.workload_clusters``
        # so the init stack and outer ``stack_azure.py`` consume both the same
        # way. A single hardcoded tenant today; a config-driven fan-out lands
        # later.
        self.workload_clusters = [
            {
                "instance": _AKS_INSTANCE_NAME,
                "cluster_name": aks.cluster_name,
                "control_plane_name": aks.control_plane_name,
                "machine_pool_name": aks.machine_pool_name,
                "control_plane_ready": aks.control_plane_ready,
                "todo": aks.todo,
            }
        ]

        self.register_outputs(
            {"workload_clusters": self.workload_clusters}
        )
