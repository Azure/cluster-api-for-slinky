"""Registry config contract for local workload-cluster components.

The outer stack forwards this shape through the init Stack CR's typed tenants
config. The local workload class consumes it directly before rendering node
bootstrap config.

The explicit ``kind`` tag is intentionally a little more structure than the
single variant needs today. It keeps the config wire format ready for future
variants such as an in-cluster Service, cloud registry, or no-mirror mode
without having to infer semantics from which keys happen to be present.
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import field_serializer, field_validator

from lib.config import PulumiConfigModel


class LocalPortRegistrySetting(PulumiConfigModel):
    """Reach the host-published local registry through a Docker gateway."""

    kind: Literal["local-port"] = "local-port"
    port: Any

    @field_serializer("kind")
    def serialize_kind(self, kind: str) -> str:
        return kind

    @field_validator("port")
    @classmethod
    def _validate_literal_port(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("port must be a positive integer")
        if isinstance(value, int):
            if value < 1:
                raise ValueError("port must be a positive integer")
            return value
        if isinstance(value, str | float):
            raise ValueError("port must be a positive integer")
        return value


RegistryConfig: TypeAlias = LocalPortRegistrySetting

