"""Per-env body for the ``local`` stack of ``ca4s-control-plane``.

Runs inside a PKO workspace pod with ``cluster-admin`` on the
management cluster (via ``pulumi-runner`` SA). Its job is to land the
tenant-AGNOSTIC management-cluster operators:

* CAPI core + the kind infrastructure provider chart
  (``infrastructure-docker``, mirroring ``capi-quickstart.yaml``).
* AWX (mirroring ``awx.yaml``). Exposed via a ``Service: LoadBalancer``
  serviced by cloud-provider-kind in local; no ingress controller in
  the picture yet.

Slinky CRDs / slurm-operator / Slurm chart are deliberately NOT in
this list: they belong on each tenant's workload cluster (that's
where ``slurm-operator`` reconciles ``NodeSet``s onto CAPI-managed
worker nodes). The per-tenant ``ca4s-workload-cluster`` stack
installs those after CAPI brings the workload cluster up.

None of the resources here touch tenant state — the per-tenant
workload-cluster stack is the one that produces per-tenant resources.

State backend
-------------
Runs against the shared ``file:///state`` PVC mounted into the
workspace pod by the outer ``PKOBootstrap``. Pulumi's
``PULUMI_CONFIG_PASSPHRASE`` arrives via ``spec.envRefs`` (a Secret
the outer also created). No backend config is needed in this module
— the workspace's environment already points Pulumi at the right
place.
"""

from __future__ import annotations

import pulumi


def run() -> None:
    """Build the control-plane stack's resource graph.

    Placeholder body. TODO: port the contents of the top-level
    ``capi-quickstart.yaml`` (CAPI core + infra-docker provider) and
    ``awx.yaml``. Each becomes a child Pulumi resource here.
    ``slurm-cluster.yaml`` is NOT mirrored here — it lands on the
    workload cluster (see ``../workload_cluster/``). No ingress
    controller for now: LoadBalancer Services on the management
    cluster get a routable IP from cloud-provider-kind directly.
    """
    # Emit a marker so ``pulumi stack output`` shows something useful
    # while the real implementation is still being built. Replace with
    # the real outputs (CAPI version, AWX URL, ...) as they land.
    pulumi.export("control_plane_ready", False)
    pulumi.export(
        "todo",
        "Install CAPI providers and AWX here.",
    )
