"""Registry config contract for workload-cluster stacks.

The outer stack writes this shape into the workload-cluster Stack CR's
``spec.config``. The inner workload-cluster program reads it back through
``pulumi.Config`` and validates it before rendering node bootstrap config.

The explicit ``kind`` tag is intentionally a little more structure than the
single variant needs today. It keeps the config wire format ready for future
variants such as an in-cluster Service, cloud registry, or no-mirror mode
without having to infer semantics from which keys happen to be present.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeAlias, TypedDict


REGISTRY_CONFIG_NAME = "registry"
REGISTRY_CONFIG_KEY = f"ca4s-workload-cluster:{REGISTRY_CONFIG_NAME}"
LOCAL_PORT_REGISTRY_KIND = "local-port"


class LocalPortRegistrySetting(TypedDict):
    """Reach the host-published local registry through a Docker gateway."""

    kind: Literal["local-port"]
    port: int


class LocalPortRegistrySettingInput(TypedDict):
    """Pulumi-input form; ``port`` may be an ``Output[int]`` while emitting CRs."""

    kind: Literal["local-port"]
    port: Any


RegistrySetting: TypeAlias = LocalPortRegistrySetting
RegistrySettingInput: TypeAlias = LocalPortRegistrySettingInput


def local_port_registry_setting(port: Any) -> RegistrySettingInput:
    """Build the local-port variant for Stack CR ``spec.config``."""
    return {"kind": LOCAL_PORT_REGISTRY_KIND, "port": port}


def parse_registry_setting(value: object | None) -> RegistrySetting | None:
    """Validate the registry setting loaded from Pulumi config."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{REGISTRY_CONFIG_KEY} must be an object with a 'kind' tag; "
            f"got {type(value).__name__}"
        )

    kind = value.get("kind")
    if kind == LOCAL_PORT_REGISTRY_KIND:
        port = value.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
            raise ValueError(
                f"{REGISTRY_CONFIG_KEY}.port must be a positive integer for "
                f"kind={LOCAL_PORT_REGISTRY_KIND!r}; got {port!r}"
            )
        return {"kind": LOCAL_PORT_REGISTRY_KIND, "port": port}

    raise ValueError(
        f"unsupported {REGISTRY_CONFIG_KEY}.kind {kind!r}; "
        f"supported values: {LOCAL_PORT_REGISTRY_KIND!r}"
    )