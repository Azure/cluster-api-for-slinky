"""Local env implementation of the PKO-owned init stack."""

from __future__ import annotations

from typing import Any

import pulumi

from stacks.control_plane.control_plane_local import ControlPlaneLocal
from stacks.workload_cluster.tenants_local import TenantsLocal


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
        del inputs

        control_plane = ControlPlaneLocal(
            "control-plane",
            opts=pulumi.ResourceOptions(parent=self),
        )
        tenants = TenantsLocal(
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
