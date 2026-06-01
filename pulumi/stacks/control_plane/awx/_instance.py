"""AWX custom resource for the management-cluster AWX instance.

Owns one ``awx.ansible.com/v1beta1`` ``AWX`` resource named ``awx`` in
the conventional ``awx`` namespace. The operator installed by
:class:`awx._operator.AWXOperator` reconciles this CR into the actual AWX
web/task/Postgres workloads, Service, and generated secrets.

This component deliberately stops at the Kubernetes object boundary. It
does not call the AWX API to create organizations, projects, credentials,
inventories, or job templates; that belongs in a later
``AWXConfiguration`` layer once the web/API service is healthy.

Local exposure uses ``Service: LoadBalancer`` because cloud-provider-kind
is already part of the local management cluster. The old quickstart
``NodePort`` + hostPath kubeconfig hack is intentionally not reproduced:
AWX has first-class Kubernetes credentials and in-cluster auth paths.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from ._operator import AWX_NAMESPACE


AWX_INSTANCE_NAME = "awx"
AWX_ADMIN_USER = "admin"
AWX_SERVICE_TYPE = "LoadBalancer"
AWX_LOADBALANCER_PORT = 80

# AWX Operator defaults to an 8Gi Postgres PVC. That's sensible for a
# persistent install but unnecessarily chunky for the local kind loop.
AWX_POSTGRES_STORAGE_SIZE = "2Gi"


class AWXInstance(pulumi.ComponentResource):
    """The AWX CR reconciled by the AWX Operator.

    Outputs:
      * ``namespace`` — namespace containing the AWX instance.
      * ``name`` — AWX CR name.
      * ``service_name`` — expected Service name (``<name>-service``).
      * ``admin_user`` — configured admin username.
      * ``admin_password_secret`` — default Secret name the operator creates
        when ``spec.admin_password_secret`` is omitted.
    """

    namespace: Output[str]
    name: Output[str]
    service_name: Output[str]
    admin_user: Output[str]
    admin_password_secret: Output[str]

    def __init__(
        self,
        name: str,
        *,
        operator: pulumi.Resource | None = None,
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:control_plane:AWXInstance", name, props={}, opts=opts)

        awx = k8s.apiextensions.CustomResource(
            f"{name}-cr",
            api_version="awx.ansible.com/v1beta1",
            kind="AWX",
            metadata={
                "name": AWX_INSTANCE_NAME,
                "namespace": AWX_NAMESPACE,
            },
            spec={
                "admin_user": AWX_ADMIN_USER,
                "service_type": AWX_SERVICE_TYPE,
                "loadbalancer_port": AWX_LOADBALANCER_PORT,
                "loadbalancer_protocol": "http",
                "projects_persistence": False,
                "postgres_storage_requirements": {
                    "requests": {"storage": AWX_POSTGRES_STORAGE_SIZE},
                },
                # Local stacks should clean up operator-generated secrets on
                # CR deletion so repeated destroy/up loops don't leave stale
                # AWX credentials behind.
                "garbage_collect_secrets": True,
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[operator] if operator is not None else None,
            ),
        )

        self.namespace = Output.from_input(AWX_NAMESPACE)
        self.name = awx.metadata["name"]
        self.service_name = Output.concat(self.name, "-service")
        self.admin_user = Output.from_input(AWX_ADMIN_USER)
        self.admin_password_secret = Output.concat(self.name, "-admin-password")

        self.register_outputs(
            {
                "namespace": self.namespace,
                "name": self.name,
                "service_name": self.service_name,
                "admin_user": self.admin_user,
                "admin_password_secret": self.admin_password_secret,
            }
        )
