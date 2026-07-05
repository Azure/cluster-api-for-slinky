"""Init-stack contract and env dispatcher.

The outer stack should own exactly one ``pulumi.com/v1`` Stack CR after PKO is
installed: ``ca4s-init``. That init stack then runs inside PKO and builds the
control plane and workload tenants for the active env.

This module is intentionally shared by both sides of that handoff:

* :class:`pko.pko_bootstrap.PKOBootstrap` calls :func:`init_stack_config` when it
  creates the single init Stack CR.
* ``pulumi/stacks/init/__main__.py`` calls :func:`run` from inside the PKO
    workspace to reconstruct :class:`stacks.stack_cr.StackCRConfig` and instantiate
  the unified init component for the active env.
"""

from __future__ import annotations

from typing import Any, Mapping, cast

import pulumi
from pydantic import ValidationError

from lib.config import PulumiConfigModel

from stacks.stack_cr import StackCRConfig, stack_cr_config_from_config
from stacks.control_plane.control_plane_config import (
    AzureInfrastructureProviderConfig,
    CONTROL_PLANE_KIND_CHILD_CONFIG_KEY,
    ControlPlaneKindConfig,
)
from stacks.control_plane.control_plane_kind import ControlPlaneKind
from stacks.workload_cluster.tenants import (
    Tenants,
    TenantsConfig,
    WorkloadClusterContext,
)


INIT_PROJECT = "ca4s-init"
INIT_REPO_DIR = "pulumi/stacks/init/"
INIT_STACK_SPEC_CONFIG_NAME = "stackSpec"
INIT_STACK_CONFIG_NAME = "initStackConfig"
INIT_STACK_SPEC_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_STACK_SPEC_CONFIG_NAME}"
INIT_STACK_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_STACK_CONFIG_NAME}"


class InitStackConfig(PulumiConfigModel):
    """Typed config PKOBootstrap forwards through the init Stack CR."""

    control_plane: ControlPlaneKindConfig = ControlPlaneKindConfig()
    tenants: TenantsConfig = TenantsConfig()


class InitStackInputs(PulumiConfigModel):
    stack_spec: StackCRConfig
    init_stack_config: InitStackConfig


class InitStack(pulumi.ComponentResource):
    """Instantiate the env-specific control plane and workload tenants."""

    control_plane_ready: pulumi.Output[bool]
    workload_clusters: list[dict[str, object]]

    def __init__(
        self,
        name: str,
        *,
        env: str,
        inputs: InitStackInputs,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:stacks:init:InitStack",
            name,
            props={},
            opts=opts,
        )

        control_plane_config = inputs.init_stack_config.control_plane
        control_plane, tenants = self._build(env, inputs, control_plane_config)

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
        env: str,
        inputs: InitStackInputs,
        control_plane_config: ControlPlaneKindConfig,
    ) -> tuple[ControlPlaneKind, Tenants]:
        stack_spec = inputs.stack_spec
        azure_provider = control_plane_config.infrastructure_providers.azure
        azure_config = (
            cast(Any, azure_provider)
            if isinstance(azure_provider, AzureInfrastructureProviderConfig)
            else None
        )

        control_plane = ControlPlaneKind(
            "control-plane",
            flux_source_namespace=stack_spec.flux_source_namespace,
            flux_source_name=stack_spec.flux_source_name,
            config=control_plane_config,
            opts=pulumi.ResourceOptions(parent=self),
        )

        tenant_context = None
        if env == "azure":
            if (
                azure_config is None
                or azure_config.identity is None
                or azure_config.default_subscription_id is None
            ):
                raise ValueError(
                    "azure init stack requires an enabled azure infrastructure "
                    "provider with identity and defaultSubscriptionId"
                )
            azure_outputs = control_plane.azure
            if azure_outputs is None:
                raise RuntimeError("InitStack requires Azure control-plane outputs")
            tenant_context = WorkloadClusterContext(
                subscription_id=str(azure_config.default_subscription_id),
                identity_name=azure_outputs.cluster_identity_name,
                identity_namespace=azure_outputs.cluster_identity_namespace,
            )
        elif env != "local":
            raise ValueError(f"unsupported init stack env {env!r}")

        tenants = Tenants(
            f"tenants-{env}",
            spec=inputs.init_stack_config.tenants,
            context=tenant_context,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[control_plane]),
        )
        return control_plane, tenants


def init_stack_config(
    *,
    stack_spec: StackCRConfig,
    init_stack_config: InitStackConfig | None = None,
) -> dict[str, Any]:
    """Build the inline config map for the single outer-owned init Stack CR."""
    return {
        INIT_STACK_SPEC_CONFIG_KEY: stack_spec.to_config(),
        INIT_STACK_CONFIG_KEY: (
            init_stack_config.model_dump(
                by_alias=True,
                mode="python",
                exclude_none=True,
                exclude_defaults=True,
            )
            if init_stack_config is not None
            else {}
        ),
    }


def parse_init_stack_spec(value: object) -> StackCRConfig:
    if not isinstance(value, Mapping):
        raise ValueError(f"{INIT_STACK_SPEC_CONFIG_KEY} must be an object")
    try:
        return stack_cr_config_from_config(dict(value))
    except ValidationError as exc:
        error = exc.errors()[0]
        config_key = ".".join(str(part) for part in error["loc"])
        raise ValueError(
            f"{INIT_STACK_SPEC_CONFIG_KEY}.{config_key}: {error['msg']}"
        ) from exc


def load_init_stack_inputs() -> InitStackInputs:
    config = pulumi.Config()
    stack_spec = parse_init_stack_spec(
        config.require_object(INIT_STACK_SPEC_CONFIG_NAME)
    )

    init_stack_config = InitStackConfig.model_validate(
        config.get_object(INIT_STACK_CONFIG_NAME) or {}
    )

    return InitStackInputs(
        stack_spec=stack_spec,
        init_stack_config=init_stack_config,
    )


def run() -> None:
    """Instantiate the init-stack component for the active env."""
    inputs = load_init_stack_inputs()
    env = pulumi.get_stack()

    init_stack = InitStack("init-stack", env=env, inputs=inputs)

    pulumi.export("control_plane_ready", init_stack.control_plane_ready)
    pulumi.export("workload_clusters", init_stack.workload_clusters)