"""Azure env implementation of the PKO-owned init stack.

Mirror of :mod:`pko._init_stack_local`. Selected by the dispatcher in
:func:`pko._init_stack.run` when the init Stack CR's env is
``azure``.

Phase 2 responsibility:

1. Unpack the child config the outer ``stack_azure.py`` sent down via
    ``PKOBootstrap(config=...)``: the UAMI identifiers
    (``childConfig.azure``) and the workload-cluster inventory
    (``childConfig.spec``).
2. Instantiate :class:`ControlPlaneAzure`, passing the typed identity spec.
3. Instantiate :class:`Tenants`, threading in the subscription ID and the
    ``AzureClusterIdentity`` name +
   namespace so the AKS control plane's ``identityRef`` resolves back to
   the Phase 1 identity.
"""

from __future__ import annotations

from typing import Any

import pulumi

from stacks.control_plane.control_plane_azure import (
    CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY,
    ControlPlaneAzure,
    parse_control_plane_azure_spec,
)
from stacks.workload_cluster.tenants import (
    SPEC_CONFIG_KEY,
    Tenants,
    WorkloadClusterContext,
)


class InitStackAzure(pulumi.ComponentResource):
    """Instantiate Azure control-plane and tenants/workload components."""

    control_plane_ready: pulumi.Output[bool]
    workload_clusters: list[dict[str, object]]

    def __init__(
        self,
        name: str,
        *,
        inputs: Any | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:InitStackAzure", name, props={}, opts=opts)

        # The dispatcher in pko._init_stack.run passes
        # ``InitStackInputs`` here; ``child_config`` is the dict the
        # outer stack threaded through ``PKOBootstrap(config=...)``.
        # Defensively handle the case where the dispatcher contract
        # changes; better to fail loudly than reconcile against an
        # empty config.
        if inputs is None or not hasattr(inputs, "child_config"):
            raise ValueError(
                "InitStackAzure requires inputs.child_config; the "
                "pko._init_stack.run() dispatcher must hand it through"
            )
        spec = parse_control_plane_azure_spec(
            inputs.child_config.get(CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY)
        )
        control_plane = ControlPlaneAzure(
            "control-plane",
            spec=spec,
            opts=pulumi.ResourceOptions(parent=self),
        )
        tenants = Tenants(
            "tenants-azure",
            spec=inputs.child_config.get(SPEC_CONFIG_KEY),
            context=WorkloadClusterContext(
                subscription_id=spec.subscription_id,
                identity_name=control_plane.azure_cluster_identity_name,
                identity_namespace=control_plane.azure_cluster_identity_namespace,
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[control_plane]),
        )

        self.control_plane_ready = control_plane.control_plane_ready
        self.workload_clusters = tenants.workload_clusters

        self.register_outputs(
            {
                "control_plane_ready": self.control_plane_ready,
                "workload_clusters": self.workload_clusters,
            }
        )
