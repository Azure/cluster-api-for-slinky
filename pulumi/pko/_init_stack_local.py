"""Local env implementation of the PKO-owned init stack."""

from __future__ import annotations

from typing import Any

import pulumi

from stacks.control_plane.control_plane_local import (
    CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY,
    ControlPlaneLocal,
    parse_control_plane_local_spec,
)
from stacks.workload_cluster.tenants import Tenants


class InitStackLocal(pulumi.ComponentResource):
    """Instantiate local control-plane and tenants/workload components."""

    control_plane_ready: pulumi.Output[bool]
    workload_clusters: list[dict[str, object]]

    def __init__(
        self,
        name: str,
        *,
        inputs: Any | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:InitStackLocal", name, props={}, opts=opts)
        stack_spec = inputs.stack_spec
        control_plane_spec = parse_control_plane_local_spec(
            inputs.child_config.get(CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY)
        )

        control_plane = ControlPlaneLocal(
            "control-plane",
            flux_source_namespace=stack_spec.pko_namespace,
            flux_source_name=stack_spec.flux_source_name,
            enable_awx=control_plane_spec.enable_awx,
            opts=pulumi.ResourceOptions(parent=self),
        )
        tenants = Tenants(
            "tenants-local",
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
