"""Tenants aggregate for the ``azure`` outer env.

Phase 1 of the CAPZ migration deliberately stops before provisioning
any workload clusters. This module exists because
:class:`pko._init_stack_azure.InitStackAzure` instantiates a
``TenantsAzure`` alongside ``ControlPlaneAzure`` by convention; without
this file the init stack would fail at import time.

When Phase 2 lands real workload clusters, this module should grow a
config-driven instance fan-out exactly like
:mod:`stacks.workload_cluster.tenants_local`. Until then,
:class:`TenantsAzure` intentionally instantiates zero child clusters
and exposes empty lists in the outputs the init stack reads.
"""

from __future__ import annotations

import pulumi


class TenantsAzure(pulumi.ComponentResource):
    """Instantiate ``azure`` workload-cluster instances (none in Phase 1).

    See module docstring for why this class is intentionally a no-op
    in Phase 1 of the CAPZ migration. The constructor signature is
    kept compatible with :class:`stacks.workload_cluster.tenants_local.TenantsLocal`
    so :class:`pko._init_stack_azure.InitStackAzure` can hand off
    without special-casing.

    Outputs:
        workload_clusters:
            Empty list. The list shape (not ``None``) is preserved so
            downstream consumers (``InitStackAzure``, the outer
            ``stack_azure.py``) don't need to special-case Phase 1.
    """

    workload_clusters: list[dict[str, object]]

    def __init__(
        self,
        name: str,
        *,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:TenantsAzure",
            name,
            props={},
            opts=opts,
        )

        self.workload_clusters = []
        self.register_outputs(
            {"workload_clusters": self.workload_clusters}
        )
