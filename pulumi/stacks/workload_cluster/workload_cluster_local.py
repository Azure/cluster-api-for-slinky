"""Per-env body for the ``local`` outer env of ``ca4s-workload-cluster``.

Takes a ``tenant`` argument from the dispatcher (which peeled it off
the second segment of the stack name ``<outer_env>-<tenant>``) and
produces, for that tenant:

1. On the management cluster (via ``pulumi-runner`` SA): a CAPI
   ``Cluster`` + control-plane config + machine deployment (mirroring
   ``capi-quickstart.yaml``). CAPI then provisions the tenant's
   workload k8s cluster on the docker infrastructure provider.
2. On the resulting workload cluster (via a second k8s provider built
   from the ``${cluster}-kubeconfig`` Secret CAPI publishes on mgmt):
   ``slurm-operator-crds`` + ``slurm-operator`` + the Slurm chart +
   per-tenant ``NodeSet``s (mirroring ``slurm-cluster.yaml`` /
   ``slurm-operator-values.yaml``). This is where the Slinky CRDs
   actually live — NOT on the management cluster.

State backend
-------------
Shared ``file:///state`` PVC, same as the other two inner stacks.
Each per-tenant stack instance gets its own subdirectory under the
PVC keyed by Pulumi's ``<org>/<project>/<stack>`` naming —
``organization/ca4s-workload-cluster/local-<tenant>/`` — so tenant
state is isolated even though the backend is shared.
"""

from __future__ import annotations

import pulumi


def run(tenant: str) -> None:
    """Build the workload-cluster resource graph for one tenant.

    Args:
        tenant: Tenant identifier (third segment of the Pulumi stack
            name's tenant half). Used to namespace / label / select
            every resource this body creates.

    Placeholder body. TODO, parameterized by ``tenant``:
      * Mgmt-cluster side: port ``capi-quickstart.yaml`` (one CAPI
        ``Cluster`` + control-plane config + machine deployment).
      * Workload-cluster side: build a second ``pulumi_kubernetes``
        provider from the ``${cluster}-kubeconfig`` Secret CAPI
        publishes on mgmt, then install ``slurm-operator-crds`` +
        ``slurm-operator`` (mirroring ``slurm-operator-values.yaml``)
        + the Slurm chart + NodeSets (mirroring ``slurm-cluster.yaml``).
    """
    # Echo the tenant + a marker so ``pulumi stack output`` confirms
    # the dispatcher routed correctly.
    pulumi.export("tenant", tenant)
    pulumi.export("workload_cluster_ready", False)
    pulumi.export(
        "todo",
        "Build CAPI Cluster on mgmt; install slurm-operator + NodeSets on workload cluster.",
    )
