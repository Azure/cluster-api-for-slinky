"""PKO init-stack contract and runtime.

The outer stack should own exactly one ``pulumi.com/v1`` Stack CR after PKO is
installed: ``ca4s-init``. That init stack then runs inside PKO, creates the
control-plane Stack CR, and instantiates the tenant/workload component after the
control-plane stack is ready.

This module is intentionally shared by both sides of that handoff:

* :class:`pko.pko_bootstrap.PKOBootstrap` calls :func:`init_stack_config` when it
  creates the single init Stack CR.
* ``pulumi/stacks/init/__main__.py`` calls :func:`run` from inside the PKO
    workspace to reconstruct :class:`pko._stack_cr.StackCRSpec`, emit the
    control-plane Stack CR, and instantiate tenant/workload resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pulumi
import pulumi_kubernetes as k8s

from pko._stack_cr import StackCRSpec, build_stack_spec
from stacks.workload_cluster.tenant_local import TenantLocal


INIT_PROJECT = "ca4s-init"
INIT_REPO_DIR = "pulumi/stacks/init/"
INIT_STACK_SPEC_CONFIG_NAME = "stackSpec"
INIT_CHILD_CONFIG_NAME = "childConfig"
INIT_STACK_SPEC_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_STACK_SPEC_CONFIG_NAME}"
INIT_CHILD_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_CHILD_CONFIG_NAME}"

CONTROL_PLANE_PROJECT = "ca4s-control-plane"
CONTROL_PLANE_REPO_DIR = "pulumi/stacks/control_plane/"
_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"

_STACK_SPEC_CONFIG_KEYS = {
    "pkoNamespace": "pko_namespace",
    "serviceAccountName": "service_account_name",
    "fluxSourceName": "flux_source_name",
    "statePvcName": "state_pvc_name",
    "stateBackendUrl": "state_backend_url",
    "passphraseSecretName": "passphrase_secret_name",
}


@dataclass(frozen=True)
class InitStackInputs:
    stack_spec: StackCRSpec
    child_config: dict[str, Any]


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
    """Create control-plane and tenant/workload resources from init."""
    inputs = load_init_stack_inputs()
    env = pulumi.get_stack()

    control_plane_spec = build_stack_spec(
        spec=inputs.stack_spec,
        project_name=CONTROL_PLANE_PROJECT,
        env=env,
        repo_dir=CONTROL_PLANE_REPO_DIR,
        config=inputs.child_config,
    )
    control_plane = k8s.apiextensions.CustomResource(
        "control-plane",
        api_version="pulumi.com/v1",
        kind="Stack",
        metadata={
            "namespace": inputs.stack_spec.pko_namespace,
            "annotations": {_WAIT_FOR_ANNOTATION: "condition=Ready"},
        },
        spec=control_plane_spec,
    )
    control_plane_stack_name = control_plane.metadata["name"]  # type: ignore[attr-defined]

    tenant = TenantLocal(
        "tenant-local",
        opts=pulumi.ResourceOptions(depends_on=[control_plane]),
    )

    pulumi.export("control_plane_stack", control_plane_stack_name)
    pulumi.export("workload_clusters", tenant.workload_clusters)
