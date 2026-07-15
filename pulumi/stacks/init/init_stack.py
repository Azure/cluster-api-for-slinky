"""Init-stack contract and config dispatcher.

The outer stack should own exactly one ``pulumi.com/v1`` Stack CR after PKO is
installed: ``ca4s-init``. That init stack then runs inside PKO and builds the
control plane and workload tenants for the active env.

This module is intentionally shared by both sides of that handoff:

* :class:`pko.pko_bootstrap.PKOBootstrap` calls :func:`init_stack_config` when it
    creates the single init Stack CR.
* ``pulumi/stacks/init/__main__.py`` calls :func:`run` from inside the PKO
    workspace to instantiate the unified init component for the active env.
"""

from __future__ import annotations

import pulumi

from lib.config import PulumiConfigModel

from stacks.control_plane.control_plane_config import ControlPlaneKindConfig
from stacks.control_plane.control_plane_kind import ControlPlaneKind
from stacks.workload_cluster.tenants import (
    Tenants,
    TenantsConfig,
    WorkloadClusterContext,
)


INIT_PROJECT = "ca4s-init"
INIT_REPO_DIR = "pulumi/stacks/init/"
INIT_STACK_CONFIG_NAME = "initStackConfig"
INIT_STACK_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_STACK_CONFIG_NAME}"


class InitStackConfig(PulumiConfigModel):
    """Typed config PKOBootstrap forwards through the init Stack CR."""

    control_plane: ControlPlaneKindConfig = ControlPlaneKindConfig()
    tenants: TenantsConfig = TenantsConfig()


class InitStack(pulumi.ComponentResource):
    """Instantiate the configured control plane and workload tenants."""

    control_plane_ready: pulumi.Output[bool]
    workload_clusters: pulumi.Output[list[dict[str, object]]]

    def __init__(
        self,
        name: str,
        *,
        config: InitStackConfig,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:stacks:init:InitStack",
            name,
            props={},
            opts=opts,
        )

        control_plane, tenants = self._build(config)

        self.control_plane_ready = control_plane.control_plane_ready
        self.workload_clusters = tenants.workload_clusters

        self.register_outputs(
            {
                "control_plane_ready": self.control_plane_ready,
                "workload_clusters": self.workload_clusters,
            }
        )

    def _build(
        self,
        config: InitStackConfig,
    ) -> tuple[ControlPlaneKind, Tenants]:
        control_plane_config = config.control_plane
        azure_provider = control_plane_config.infrastructure_providers.azure
        azure_config = (
            azure_provider
            if azure_provider is not None and azure_provider.enabled
            else None
        )

        control_plane = ControlPlaneKind(
            "control-plane",
            config=control_plane_config,
            opts=pulumi.ResourceOptions(parent=self),
        )

        tenant_context: pulumi.Input[WorkloadClusterContext] | None = None
        if azure_config is not None:
            azure_outputs = control_plane.azure
            tenant_context = (
                azure_outputs.apply(
                    lambda outputs: WorkloadClusterContext(
                        identity_name=outputs.cluster_identity_name,
                        identity_namespace=outputs.cluster_identity_namespace,
                        azure_client_id=outputs.client_id,
                        azure_tenant_id=outputs.tenant_id,
                        azure_identity_resource_id=outputs.resource_id,
                    )
                )
                if azure_outputs is not None
                else None
            )

        tenants = Tenants(
            "tenants",
            spec=config.tenants,
            context=tenant_context,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[control_plane]),
        )
        return control_plane, tenants


def run() -> None:
    """Instantiate the init-stack component for the active env."""
    pulumi_config = pulumi.Config()
    config = InitStackConfig.model_validate(
        pulumi_config.get_object(INIT_STACK_CONFIG_NAME) or {}
    )

    init_stack = InitStack("init-stack", config=config)

    pulumi.export("control_plane_ready", init_stack.control_plane_ready)
    pulumi.export("workload_clusters", init_stack.workload_clusters)
