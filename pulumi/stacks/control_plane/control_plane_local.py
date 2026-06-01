"""Per-env body for the ``local`` stack of ``ca4s-control-plane``.

Runs inside a PKO workspace pod with ``cluster-admin`` on the
management cluster (via ``pulumi-runner`` SA). Its job is to land the
tenant-AGNOSTIC management-cluster operators:

* CAPI core + the kind infrastructure provider chart
  (``infrastructure-docker``, mirroring ``capi-quickstart.yaml``).
* AWX (successor to ``awx.yaml``). Exposed via a ``Service: LoadBalancer``
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

from awx import AWXInstance, AWXOperator
from certmanager import CertManager


def run() -> None:
    """Build the control-plane stack's resource graph.

    First wave landed: foundational operators that everything else
    depends on.

    * :class:`certmanager.CertManager` — cert-manager + CRDs, a
      prerequisite for the CAPI operator's webhooks (and AWX ingress
      TLS).
    * :class:`awx.AWXOperator` — the AWX operator + ``AWX`` CRD.
    * :class:`awx.AWXInstance` — the ``AWX`` CR reconciled by the
      operator into web/task/Postgres workloads and a LoadBalancer Service.

    cert-manager and the AWX operator are independent, so they install in
    parallel. The AWX instance waits for the operator release so the CRD and
    controller exist before the ``AWX`` CR is submitted.

    Still TODO: the CAPI pieces (``cluster-api-operator`` +
    ``CoreProvider``/``BootstrapProvider``/``ControlPlaneProvider``/
    ``InfrastructureProvider`` CRs + the ``ClusterClass`` and templates
    from ``capi-quickstart.yaml``), AWX API configuration, and the
    cluster-autoscaler. ``slurm-cluster.yaml`` is
    NOT mirrored here — it lands on the workload cluster (see
    ``../workload_cluster/``). No ingress controller for now:
    LoadBalancer Services on the management cluster get a routable IP
    from cloud-provider-kind directly.

    Runs inside a PKO workspace pod with ``cluster-admin`` via the
    ``pulumi-runner`` SA, so resources use the pod's ambient in-cluster
    kubeconfig (no explicit provider).
    """
    cert_manager = CertManager("cert-manager")
    awx_operator = AWXOperator("awx-operator")
    awx_instance = AWXInstance("awx-instance", operator=awx_operator)

    pulumi.export("cert_manager_namespace", cert_manager.namespace)
    pulumi.export("awx_operator_namespace", awx_operator.namespace)
    pulumi.export("awx_instance_name", awx_instance.name)
    pulumi.export("awx_service_name", awx_instance.service_name)
    pulumi.export("awx_admin_user", awx_instance.admin_user)
    pulumi.export(
        "awx_admin_password_secret", awx_instance.admin_password_secret
    )

    # Flip to True once the remaining CAPI pieces and AWX API config land.
    pulumi.export("control_plane_ready", False)
    pulumi.export(
        "todo",
        "Install CAPI operator + providers + ClusterClass and AWX API config.",
    )
