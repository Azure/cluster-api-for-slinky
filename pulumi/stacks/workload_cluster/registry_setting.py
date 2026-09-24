# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Automatic local registry coordinates and explicit containerd route overrides."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import AfterValidator, StringConstraints, field_serializer, field_validator, model_validator

from lib.config import NonEmptyStr, PulumiConfigModel


RegistryName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]


def _registry_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("registry URL must use http or https without credentials")
    if parsed.query or parsed.fragment or any(character.isspace() for character in value):
        raise ValueError("registry URL must not contain whitespace, a query, or fragment")
    return value


RegistryURL = Annotated[NonEmptyStr, AfterValidator(_registry_url)]


class ContainerdHostConfig(PulumiConfigModel):
    """A direct registry URL or a host-published port reached via Docker's gateway."""

    url: RegistryURL | None = None
    gateway_port: Any = None
    scheme: Literal["http", "https"] = "http"
    capabilities: tuple[Literal["pull", "resolve", "push"], ...] = ("pull", "resolve")

    @model_validator(mode="after")
    def validate_host(self):
        if (self.url is None) == (self.gateway_port is None):
            raise ValueError("exactly one of url or gatewayPort is required")
        if self.gateway_port is not None:
            LocalPortRegistrySetting(port=self.gateway_port)
        if not self.capabilities or len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be nonempty and unique")
        return self


class ContainerdRegistryConfig(PulumiConfigModel):
    """One registry namespace's hosts.toml; no hosts means direct server access."""

    server: RegistryURL
    hosts: tuple[ContainerdHostConfig, ...] = ()

    @model_validator(mode="after")
    def validate_hosts(self):
        endpoints = [(host.url, host.gateway_port, host.scheme) for host in self.hosts]
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("registry hosts must be unique")
        return self


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


class LocalCustomRegistrySetting(PulumiConfigModel):
    """Reach a named ctlptl registry through its host-published port."""

    registry_name: RegistryName
    port: Any

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

