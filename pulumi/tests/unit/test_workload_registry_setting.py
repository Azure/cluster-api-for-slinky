from __future__ import annotations

import pytest

from stacks.workload_cluster.registry_setting import (
    LOCAL_PORT_REGISTRY_KIND,
    REGISTRY_CONFIG_KEY,
    local_port_registry_setting,
    parse_registry_setting,
)


def test_local_port_registry_setting_round_trips() -> None:
    setting = local_port_registry_setting(5002)

    assert setting == {"kind": LOCAL_PORT_REGISTRY_KIND, "port": 5002}
    assert parse_registry_setting(setting) == setting


@pytest.mark.parametrize("port", [0, -1, True, "5002"])
def test_local_port_registry_setting_rejects_invalid_ports(port: object) -> None:
    with pytest.raises(ValueError, match=f"{REGISTRY_CONFIG_KEY}.port"):
        parse_registry_setting({"kind": LOCAL_PORT_REGISTRY_KIND, "port": port})


def test_registry_setting_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parse_registry_setting({"kind": "service", "name": "registry"})