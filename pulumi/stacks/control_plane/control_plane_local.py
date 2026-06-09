"""Per-env control-plane component for the ``local`` env.

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
worker nodes). ``TenantsLocal`` installs those after CAPI brings the
workload cluster up.

None of the resources here touch tenant state — tenants/workload components
produce per-tenant resources.

State backend
-------------
Runs inside the PKO-owned ``ca4s-init`` stack, so control-plane resources share
that stack's ``file:///state`` backend. A separate control-plane Stack boundary
can be reintroduced later if isolated lifecycle/state becomes useful.
"""

from __future__ import annotations

import pulumi

try:
    from .awx import AWXInstance, AWXOperator
    from .capi import ClusterAPIOperator
    from .certmanager import CertManager
except ImportError:
    from awx import AWXInstance, AWXOperator
    from capi import ClusterAPIOperator
    from certmanager import CertManager


class ControlPlaneLocal(pulumi.ComponentResource):
    """Build the local control-plane resource graph."""

    cert_manager_namespace: pulumi.Output[str]
    capi_operator_namespace: pulumi.Output[str]
    capi_provider_version: pulumi.Output[str]
    capi_provider_namespaces: dict[str, pulumi.Output[str]]
    awx_operator_namespace: pulumi.Output[str]
    awx_instance_name: pulumi.Output[str]
    awx_service_name: pulumi.Output[str]
    awx_admin_user: pulumi.Output[str]
    awx_admin_password_secret: pulumi.Output[str]
    control_plane_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ControlPlaneLocal",
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
            opts=child_options(),
        )
        awx_operator = AWXOperator("awx-operator", opts=child_options())
        # TODO(awx-plumbing): keep AWX installed as a management-plane surface,
        # but defer wiring it until the CAPI/CAPD path is solid. Useful
        # follow-ups: scoped Kubernetes credential, AWX org/project/inventory,
        # job templates for day-2 operations, and tenant credential boundaries.
        awx_instance = AWXInstance(
            "awx-instance",
            operator=awx_operator,
            opts=child_options(),
        )

        self.cert_manager_namespace = cert_manager.namespace
        self.capi_operator_namespace = capi.namespace
        self.capi_provider_version = capi.provider_version
        self.capi_provider_namespaces = capi.provider_namespaces
        self.awx_operator_namespace = awx_operator.namespace
        self.awx_instance_name = awx_instance.name
        self.awx_service_name = awx_instance.service_name
        self.awx_admin_user = awx_instance.admin_user
        self.awx_admin_password_secret = awx_instance.admin_password_secret
        self.control_plane_ready = pulumi.Output.from_input(False)
        self.todo = pulumi.Output.from_input(
            "Wire AWX API config and tenant-facing Slurm day-2 operations."
        )

        self.register_outputs(
            {
                "cert_manager_namespace": self.cert_manager_namespace,
                "capi_operator_namespace": self.capi_operator_namespace,
                "capi_provider_version": self.capi_provider_version,
                "capi_provider_namespaces": self.capi_provider_namespaces,
                "awx_operator_namespace": self.awx_operator_namespace,
                "awx_instance_name": self.awx_instance_name,
                "awx_service_name": self.awx_service_name,
                "awx_admin_user": self.awx_admin_user,
                "awx_admin_password_secret": self.awx_admin_password_secret,
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
