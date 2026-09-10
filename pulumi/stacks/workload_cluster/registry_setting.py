# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Registry config contract for local workload-cluster components."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from lib.config import NonEmptyStr, PulumiConfigModel


_REGISTRY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class ContainerdHostConfig(PulumiConfigModel):
    """A containerd registry host reached through the Docker gateway."""

    gateway_port: Any
    scheme: Literal["http", "https"] = "http"
    capabilities: tuple[Literal["pull", "resolve", "push"], ...] = (
        "pull",
        "resolve",
    )

    @field_validator("gateway_port")
    @classmethod
    def validate_gateway_port(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("gatewayPort must be a positive integer")
        if isinstance(value, int):
            if value < 1:
                raise ValueError("gatewayPort must be a positive integer")
            return value
        if isinstance(value, str | float):
            raise ValueError("gatewayPort must be a positive integer")
        return value

    @model_validator(mode="after")
    def validate_capabilities(self) -> ContainerdHostConfig:
        if not self.capabilities:
            raise ValueError("capabilities must not be empty")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must be unique")
        return self


class ContainerdRegistryConfig(PulumiConfigModel):
    """Typed representation of one containerd registry ``hosts.toml`` file."""

    server: NonEmptyStr
    hosts: tuple[ContainerdHostConfig, ...] = Field(min_length=1)


class LocalRegistryConfig(PulumiConfigModel):
    """Containerd configuration for one registry namespace."""

    registry: NonEmptyStr
    config: ContainerdRegistryConfig

    @field_validator("registry")
    @classmethod
    def validate_registry(cls, value: str) -> str:
        if not _REGISTRY_PATTERN.fullmatch(value):
            raise ValueError("registry must be a host name with an optional port")
        return value

