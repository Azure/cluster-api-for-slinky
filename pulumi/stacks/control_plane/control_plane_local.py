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
    from .awx import AWXInstance, AWXOperator, AWXProviderConfig
    from .awx._configuration import AWXConfiguration
    from .capi import ClusterAPIOperator
    from .certmanager import CertManager
except ImportError:
    from awx import AWXInstance, AWXOperator, AWXProviderConfig
    from awx._configuration import AWXConfiguration
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
    awx_api_url: pulumi.Output[str]
    awx_admin_user: pulumi.Output[str]
    awx_admin_password: pulumi.Output[str]
    awx_admin_password_secret: pulumi.Output[str]
    awx_provider: pulumi.ProviderResource
    awx_organization_id: pulumi.Output[float]
    awx_project_id: pulumi.Output[float]
    awx_project_name: pulumi.Output[str]
    awx_scm_credential_id: pulumi.Output[float]
    control_plane_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        flux_source_namespace: pulumi.Input[str],
        flux_source_name: pulumi.Input[str],
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
        awx_instance = AWXInstance(
            "awx-instance",
            operator=awx_operator,
            opts=child_options(),
        )
        awx_provider_config = AWXProviderConfig(
            "awx-api",
            instance=awx_instance,
            opts=child_options(),
        )
        awx_configuration = AWXConfiguration(
            "awx-configuration",
            provider_config=awx_provider_config,
            flux_source_namespace=flux_source_namespace,
            flux_source_name=flux_source_name,
            opts=child_options(),
        )

        self.cert_manager_namespace = cert_manager.namespace
        self.capi_operator_namespace = capi.namespace
        self.capi_provider_version = capi.provider_version
        self.capi_provider_namespaces = capi.provider_namespaces
        self.awx_operator_namespace = awx_operator.namespace
        self.awx_instance_name = awx_instance.name
        self.awx_service_name = awx_instance.service_name
        self.awx_api_url = awx_provider_config.api_url
        self.awx_admin_user = awx_instance.admin_user
        self.awx_admin_password = awx_provider_config.admin_password
        self.awx_admin_password_secret = awx_instance.admin_password_secret
        self.awx_provider = awx_provider_config.provider
        self.awx_organization_id = awx_configuration.organization_id
        self.awx_project_id = awx_configuration.project_id
        self.awx_project_name = awx_configuration.project_name
        self.awx_scm_credential_id = awx_configuration.scm_credential_id
        self.control_plane_ready = pulumi.Output.all(
            capi.provider_version,
            awx_configuration.project_id,
        ).apply(lambda _: True)
        self.todo = pulumi.Output.from_input(
            "Wire AWX tenant inventories, credentials, and Slurm day-2 job templates."
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
                "awx_api_url": self.awx_api_url,
                "awx_admin_user": self.awx_admin_user,
                "awx_admin_password": self.awx_admin_password,
                "awx_admin_password_secret": self.awx_admin_password_secret,
                "awx_organization_id": self.awx_organization_id,
                "awx_project_id": self.awx_project_id,
                "awx_project_name": self.awx_project_name,
                "awx_scm_credential_id": self.awx_scm_credential_id,
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
