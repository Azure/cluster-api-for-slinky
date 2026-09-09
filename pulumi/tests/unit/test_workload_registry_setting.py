# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import pytest

from stacks.workload_cluster.registry_setting import (
    ContainerdHostConfig,
    ContainerdRegistryConfig,
    LocalRegistryConfig,
)
from stacks.workload_cluster.workload_cluster_class_local import (
    LocalWorkloadClusterConfig,
)


def test_local_registry_model_round_trips() -> None:
    setting = LocalRegistryConfig(
        registry="custom-registry:5000",
        config=ContainerdRegistryConfig(
            server="http://custom-registry:5000",
            hosts=(
                ContainerdHostConfig(
                    gateway_port=5002,
                    capabilities=("pull", "resolve", "push"),
                ),
            ),
        ),
    ).to_config()
    parsed = LocalRegistryConfig.model_validate(setting)

    assert setting == {
        "registry": "custom-registry:5000",
        "config": {
            "server": "http://custom-registry:5000",
            "hosts": [
                {
                    "gatewayPort": 5002,
                    "capabilities": ["pull", "resolve", "push"],
                }
            ],
        },
    }
    assert parsed.to_config() == setting


@pytest.mark.parametrize("port", [0, -1, True, "5002"])
def test_containerd_host_model_rejects_invalid_ports(port: object) -> None:
    with pytest.raises(ValueError, match="gatewayPort must be a positive integer"):
        ContainerdHostConfig.model_validate({"gatewayPort": port})


def test_containerd_host_model_rejects_duplicate_capabilities() -> None:
    with pytest.raises(ValueError, match="capabilities must be unique"):
        ContainerdHostConfig(capabilities=("pull", "pull"), gateway_port=5002)


def test_containerd_registry_model_requires_a_host() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        ContainerdRegistryConfig(
            server="https://registry.example",
            hosts=(),
        )


def test_local_registry_model_rejects_path_as_registry() -> None:
    with pytest.raises(ValueError, match="host name with an optional port"):
        LocalRegistryConfig(
            registry="registry.example/project",
            config=ContainerdRegistryConfig(
                server="https://registry.example",
                hosts=(ContainerdHostConfig(gateway_port=5002),),
            ),
        )


def test_local_workload_config_rejects_duplicate_registries() -> None:
    registry = LocalRegistryConfig(
        registry="docker.io",
        config=ContainerdRegistryConfig(
            server="https://registry-1.docker.io",
            hosts=(ContainerdHostConfig(gateway_port=5002),),
        ),
    )

    with pytest.raises(ValueError, match="unique registry names"):
        LocalWorkloadClusterConfig(registries=(registry, registry))