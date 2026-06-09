"""Per-env body for the ``local`` stack of ``ca4s-control-plane``.

Runs inside a PKO workspace pod with ``cluster-admin`` on the
management cluster (via ``pulumi-runner`` SA). Its job is to land the
tenant-AGNOSTIC management-cluster operators:

* Cluster API Operator + the core, kubeadm bootstrap, kubeadm control-plane,
  and Docker infrastructure providers.
* AWX (successor to ``awx.yaml``). Exposed via a ``Service: LoadBalancer``
  serviced by cloud-provider-kind in local; no ingress controller in
  the picture yet.

Slinky CRDs / slurm-operator / Slurm chart are deliberately NOT in
this list: they belong on each tenant's workload cluster (that's
where ``slurm-operator`` reconciles ``NodeSet``s onto CAPI-managed
worker nodes). ``TenantLocal`` installs those after CAPI brings the
workload cluster up.

None of the resources here touch tenant state — tenant/workload components
produce per-tenant resources.

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
from capi import ClusterAPIOperator
from certmanager import CertManager


def run() -> None:
    """Build the control-plane stack's resource graph.

    First wave landed: foundational operators that everything else
    depends on.

    * :class:`certmanager.CertManager` — cert-manager + CRDs, a
      prerequisite for the CAPI operator's webhooks (and AWX ingress
      TLS).
    * :class:`capi.ClusterAPIOperator` — Cluster API Operator plus
      ``cluster-api``, ``kubeadm`` bootstrap/control-plane, and ``docker``
      infrastructure provider CRs.
    * :class:`awx.AWXOperator` — the AWX operator + ``AWX`` CRD.
    * :class:`awx.AWXInstance` — the ``AWX`` CR reconciled by the
      operator into web/task/Postgres workloads and a LoadBalancer Service.

    cert-manager, CAPI, and the AWX operator are mostly independent, so they
    install in parallel. The AWX instance waits for the operator release so
    the CRD and controller exist before the ``AWX`` CR is submitted.

    Still TODO here: AWX API configuration and the cluster-autoscaler.
    ``ClusterClass`` / templates from ``capi-quickstart.yaml`` and
    ``slurm-cluster.yaml`` are NOT mirrored here — cluster shape and workload
    contents land through the workload-cluster path (see
    ``../workload_cluster/``). No ingress controller for now:
    LoadBalancer Services on the management cluster get a routable IP
    from cloud-provider-kind directly.

    Runs inside a PKO workspace pod with ``cluster-admin`` via the
    ``pulumi-runner`` SA, so resources use the pod's ambient in-cluster
    kubeconfig (no explicit provider).
    """
    cert_manager = CertManager("cert-manager")
    capi = ClusterAPIOperator("cluster-api", cert_manager=cert_manager)
    awx_operator = AWXOperator("awx-operator")
    awx_instance = AWXInstance("awx-instance", operator=awx_operator)

    # TODO(awx-plumbing): keep AWX installed as a management-plane surface, but
    # defer wiring it until the CAPI/CAPD path is solid. Useful follow-ups:
    # create a scoped ServiceAccount/token credential so AWX can reach this
    # cluster reflexively through the Kubernetes API; create the matching AWX
    # credential via the AWX API; register the GitOps repo/project as an AWX
    # project/source; sync a CAPI-derived inventory from workload clusters;
    # add job templates for day-2 operations; decide how AWX credentials map to
    # tenant boundaries; and expose any AWX URL/admin details needed by users.

    pulumi.export("cert_manager_namespace", cert_manager.namespace)
    pulumi.export("capi_operator_namespace", capi.namespace)
    pulumi.export("capi_provider_version", capi.provider_version)
    pulumi.export("capi_provider_namespaces", capi.provider_namespaces)
    pulumi.export("awx_operator_namespace", awx_operator.namespace)
    pulumi.export("awx_instance_name", awx_instance.name)
    pulumi.export("awx_service_name", awx_instance.service_name)
    pulumi.export("awx_admin_user", awx_instance.admin_user)
    pulumi.export("awx_admin_password_secret", awx_instance.admin_password_secret)

    # Flip to True once the remaining CAPI pieces and AWX API config land.
    pulumi.export("control_plane_ready", False)
    pulumi.export(
        "todo",
        "Wire AWX API config and cluster-autoscaler; ClusterClass lives in workload stack.",
    )
