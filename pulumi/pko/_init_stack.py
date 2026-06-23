"""PKO init-stack contract and env dispatcher.

The outer stack should own exactly one ``pulumi.com/v1`` Stack CR after PKO is
installed: ``ca4s-init``. That init stack then runs inside PKO and dispatches to
an env-specific init component such as :class:`pko._init_stack_local.InitStackLocal`.

This module is intentionally shared by both sides of that handoff:

* :class:`pko.pko_bootstrap.PKOBootstrap` calls :func:`init_stack_config` when it
  creates the single init Stack CR.
* ``pulumi/stacks/init/__main__.py`` calls :func:`run` from inside the PKO
    workspace to reconstruct :class:`pko._stack_cr.StackCRSpec` and instantiate
    the env-specific init component.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any, Mapping

import pulumi

from pko._stack_cr import StackCRSpec


INIT_PROJECT = "ca4s-init"
INIT_REPO_DIR = "pulumi/stacks/init/"
INIT_STACK_SPEC_CONFIG_NAME = "stackSpec"
INIT_CHILD_CONFIG_NAME = "childConfig"
INIT_STACK_SPEC_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_STACK_SPEC_CONFIG_NAME}"
INIT_CHILD_CONFIG_KEY = f"{INIT_PROJECT}:{INIT_CHILD_CONFIG_NAME}"

_INIT_STACK_MODULE_PREFIX = "pko._init_stack_"
_INIT_STACK_CLASS_PREFIX = "InitStack"

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


def _pascal_case(value: str) -> str:
    words = value.replace("-", "_").split("_")
    return "".join(word.capitalize() for word in words if word)


def run() -> None:
    """Instantiate the env-specific init-stack component."""
    inputs = load_init_stack_inputs()
    env = pulumi.get_stack()

    module_name = f"{_INIT_STACK_MODULE_PREFIX}{env}"
    class_name = f"{_INIT_STACK_CLASS_PREFIX}{_pascal_case(env)}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ValueError(
            f"unsupported init stack env {env!r}: expected module {module_name!r} "
            f"exposing class {class_name!r}."
        ) from None

    try:
        concrete = getattr(module, class_name)
    except AttributeError:
        raise ValueError(
            f"module {module_name!r} does not expose class {class_name!r}; "
            "env-specific init-stack modules must follow InitStack<Env>."
        ) from None

    init_stack = concrete("init-stack", inputs=inputs)

    pulumi.export("control_plane_ready", init_stack.control_plane_ready)
    pulumi.export("workload_clusters", init_stack.workload_clusters)
