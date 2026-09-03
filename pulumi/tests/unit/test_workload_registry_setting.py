# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import pytest

from stacks.workload_cluster.registry_setting import (
    LocalCustomRegistrySetting,
    LocalPortRegistrySetting,
)


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