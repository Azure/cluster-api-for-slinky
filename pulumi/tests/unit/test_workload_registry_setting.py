# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import pytest

from stacks.workload_cluster.registry_setting import (
    ContainerdHostConfig,
    ContainerdRegistryConfig,
    LocalCustomRegistrySetting,
    LocalPortRegistrySetting,
)
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig


def test_registry_routes_round_trip_with_multiple_hosts():
    config = LocalWorkloadClusterConfig.model_validate({"registryRoutes": {
        "private.example:5000": {"server": "https://private.example:5000", "hosts": [
            {"url": "https://mirror.example", "capabilities": ["pull"]},
            {"gatewayPort": 5443, "scheme": "https", "capabilities": ["pull", "resolve", "push"]},
        ]},
    }})
    assert LocalWorkloadClusterConfig.model_validate(config.to_config()) == config


@pytest.mark.parametrize("host", [
    {}, {"url": "https://mirror.example", "gatewayPort": 5002},
    {"gatewayPort": 0}, {"url": "file:///etc/hosts"}, {"url": "https://user:password@mirror.example"},
    {"url": "https://mirror.example", "capabilities": []},
    {"gatewayPort": 5002, "capabilities": ["pull", "pull"]},
])
def test_registry_route_rejects_invalid_hosts(host):
    with pytest.raises(ValueError):
        ContainerdHostConfig.model_validate(host)


def test_registry_routes_reject_duplicate_hosts_and_unsafe_names():
    with pytest.raises(ValueError, match="unique"):
        ContainerdRegistryConfig(server="https://registry.example", hosts=(
            ContainerdHostConfig(gateway_port=5002), ContainerdHostConfig(gateway_port=5002),
        ))
    for name in ("../registry", "registry;echo bad", "registry/path"):
        with pytest.raises(ValueError):
            LocalWorkloadClusterConfig.model_validate({"registryRoutes": {name: {"server": "https://registry.example"}}})
        with pytest.raises(ValueError):
            LocalCustomRegistrySetting(registry_name=name, port=5002)


def test_local_port_registry_model_round_trips() -> None:
    setting = LocalPortRegistrySetting(port=5002).to_config()
    parsed = LocalPortRegistrySetting.model_validate(setting)

    assert setting == {"kind": "local-port", "port": 5002}
    assert parsed is not None
    assert parsed.to_config() == setting


@pytest.mark.parametrize("port", [0, -1, True, "5002"])
def test_local_port_registry_model_rejects_invalid_ports(port: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        LocalPortRegistrySetting.model_validate({"kind": "local-port", "port": port})


def test_registry_setting_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        LocalPortRegistrySetting.model_validate({"kind": "service", "name": "registry"})


def test_local_custom_registry_model_round_trips() -> None:
    setting = LocalCustomRegistrySetting(
        registry_name="custom-registry",
        port=5003,
    ).to_config()
    parsed = LocalCustomRegistrySetting.model_validate(setting)

    assert setting == {"registryName": "custom-registry", "port": 5003}
    assert parsed.to_config() == setting


@pytest.mark.parametrize("port", [0, -1, True, "5003"])
def test_local_custom_registry_rejects_invalid_ports(port: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        LocalCustomRegistrySetting.model_validate(
            {"registryName": "custom-registry", "port": port}
        )