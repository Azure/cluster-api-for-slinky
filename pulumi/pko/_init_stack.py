"""PKO init-stack contract and env dispatcher.

The outer stack should own exactly one ``pulumi.com/v1`` Stack CR after PKO is
installed: ``ca4s-init``. That init stack then runs inside PKO and builds the
control plane and workload tenants for the active env.

This module is intentionally shared by both sides of that handoff:

* :class:`pko.pko_bootstrap.PKOBootstrap` calls :func:`init_stack_config` when it
  creates the single init Stack CR.
* ``pulumi/stacks/init/__main__.py`` calls :func:`run` from inside the PKO
    workspace to reconstruct :class:`pko._stack_cr.StackCRSpec` and instantiate
    the unified init component for the active env.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pulumi

from pko._stack_cr import StackCRSpec
from stacks.control_plane.control_plane_config import (
    CONTROL_PLANE_KIND_CHILD_CONFIG_KEY,
    ControlPlaneKindConfig,
    parse_control_plane_kind_config,
)
from stacks.control_plane.control_plane_kind import (
    ControlPlaneKind,
    ControlPlaneKindSpec,
    KindAzureControlPlaneSpec,
)
from stacks.workload_cluster.tenants import (
    SPEC_CONFIG_KEY,
    Tenants,
    WorkloadClusterContext,
)


INIT_PROJECT = "ca4s-init"
INIT_REPO_DIR = "pulumi/stacks/init/"
INIT_STACK_SPEC_CONFIG_NAME = "stackSpec"
INIT_CHILD_CONFIG_NAME = "childConfig"
INIT_STACK_SPEC_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_STACK_SPEC_CONFIG_NAME}"
INIT_CHILD_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_CHILD_CONFIG_NAME}"

_STACK_SPEC_CONFIG_KEYS = {
    "pkoNamespace": "pko_namespace",
    "serviceAccountName": "service_account_name",
    "fluxSourceName": "flux_source_name",
    "fluxSourceNamespace": "flux_source_namespace",
    "statePvcName": "state_pvc_name",
    "stateBackendUrl": "state_backend_url",
    "passphraseSecretName": "passphrase_secret_name",
}


@dataclass(frozen=True)
class InitStackInputs:
    stack_spec: StackCRSpec
    # TODO: Replace this raw handoff dict with typed init-stack input objects
    # as the control-plane, registry, and workload config contracts stabilize.
    child_config: dict[str, Any]


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
            "ca4s:pko:InitStack",
            name,
            props={},
            opts=opts,
        )

        control_plane_config = parse_control_plane_kind_config(
            inputs.child_config.get(CONTROL_PLANE_KIND_CHILD_CONFIG_KEY)
        )
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
        azure_config = control_plane_config.azure

        control_plane = ControlPlaneKind(
            "control-plane",
            flux_source_namespace=stack_spec.flux_source_namespace,
            flux_source_name=stack_spec.flux_source_name,
            spec=ControlPlaneKindSpec(
                infrastructure_providers=control_plane_config.infrastructure_providers,
                enable_awx=control_plane_config.enable_awx,
                azure=(
                    KindAzureControlPlaneSpec(
                        client_id=azure_config.client_id,
                        principal_id=azure_config.principal_id,
                        tenant_id=azure_config.tenant_id,
                        subscription_id=azure_config.subscription_id,
                        allowed_namespaces=azure_config.allowed_namespaces,
                        skip_in_cluster_preflight=(
                            azure_config.skip_in_cluster_preflight
                        ),
                    )
                    if azure_config is not None
                    else None
                ),
            ),
            opts=pulumi.ResourceOptions(parent=self),
        )

        tenant_context = None
        if env == "azure":
            if azure_config is None:
                raise ValueError("azure init stack requires controlPlane.azure config")
            azure_outputs = control_plane.azure
            if azure_outputs is None:
                raise RuntimeError("InitStack requires Azure control-plane outputs")
            tenant_context = WorkloadClusterContext(
                subscription_id=azure_config.subscription_id,
                identity_name=azure_outputs.cluster_identity_name,
                identity_namespace=azure_outputs.cluster_identity_namespace,
            )
        elif env != "local":
            raise ValueError(f"unsupported init stack env {env!r}")

        tenants = Tenants(
            f"tenants-{env}",
            spec=inputs.child_config.get(SPEC_CONFIG_KEY),
            context=tenant_context,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[control_plane]),
        )
        return control_plane, tenants


def init_stack_config(
    *,
    stack_spec: StackCRSpec,
    child_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the inline config map for the single outer-owned init Stack CR."""
    return {
        INIT_STACK_SPEC_CONFIG_KEY: {
            "pkoNamespace": stack_spec.pko_namespace,
            "serviceAccountName": stack_spec.service_account_name,
            "fluxSourceName": stack_spec.flux_source_name,
            "fluxSourceNamespace": stack_spec.flux_source_namespace,
            "statePvcName": stack_spec.state_pvc_name,
            "stateBackendUrl": stack_spec.state_backend_url,
            "passphraseSecretName": stack_spec.passphrase_secret_name,
        },
        INIT_CHILD_CONFIG_KEY: child_config or {},
    }


def parse_init_stack_spec(value: object) -> StackCRSpec:
    if not isinstance(value, Mapping):
        raise ValueError(f"{INIT_STACK_SPEC_CONFIG_KEY} must be an object")

    parsed: dict[str, str] = {}
    for config_key, field_name in _STACK_SPEC_CONFIG_KEYS.items():
        field_value = value.get(config_key)
        if not isinstance(field_value, str) or not field_value:
            raise ValueError(
                f"{INIT_STACK_SPEC_CONFIG_KEY}.{config_key} must be a non-empty string"
            )
        parsed[field_name] = field_value

    return StackCRSpec(**parsed)


def load_init_stack_inputs() -> InitStackInputs:
    config = pulumi.Config()
    stack_spec = parse_init_stack_spec(
        config.require_object(INIT_STACK_SPEC_CONFIG_NAME)
    )

    child_config = config.get_object(INIT_CHILD_CONFIG_NAME) or {}
    if not isinstance(child_config, dict):
        raise ValueError(f"{INIT_CHILD_CONFIG_KEY} must be an object")

    return InitStackInputs(stack_spec=stack_spec, child_config=child_config)


def run() -> None:
    """Instantiate the init-stack component for the active env."""
    inputs = load_init_stack_inputs()
    env = pulumi.get_stack()

    init_stack = InitStack("init-stack", env=env, inputs=inputs)

    pulumi.export("control_plane_ready", init_stack.control_plane_ready)
    pulumi.export("workload_clusters", init_stack.workload_clusters)
