"""Unit tests for :mod:`ctlptl.ctlptl_registry_service`."""

from __future__ import annotations

import json
import subprocess

import pytest

from ctlptl.ctlptl_registry_service import CtlptlRegistryService  # noqa: F401
from ctlptl import ctlptl_registry_service


def _docker_inspect(ip_address: str = "172.18.0.5") -> str:
    return json.dumps(
        [
            {
                "NetworkSettings": {
                    "Networks": {
                        "bridge": {"IPAddress": "172.17.0.2"},
                        "kind": {"IPAddress": ip_address},
                    }
                }
            }
        ]
    )


def test_container_network_ip_reads_kind_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ctlptl_registry_service.shutil, "which", lambda name: name)

    def fake_run(
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_docker_inspect(), stderr="")

    monkeypatch.setattr(ctlptl_registry_service.subprocess, "run", fake_run)

    assert (
        ctlptl_registry_service._container_network_ip(
            "custom-registry",
            "kind",
        )
        == "172.18.0.5"
    )
    assert calls == [["docker", "inspect", "custom-registry"]]


def test_container_network_ip_requires_attached_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctlptl_registry_service.shutil, "which", lambda name: name)

    def fake_run(
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=_docker_inspect(), stderr="")

    monkeypatch.setattr(ctlptl_registry_service.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="not attached"):
        ctlptl_registry_service._container_network_ip(
            "custom-registry",
            "missing-network",
        )


def test_provider_create_outputs_ip_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ctlptl_registry_service,
        "_container_network_ip",
        lambda container_name, network_name: "172.18.0.6",
    )

    result = ctlptl_registry_service._CtlptlRegistryNetworkAddressProvider().create(
        {"container_name": "custom-registry"}
    )

    assert result.id == "custom-registry:kind"
    assert result.outs is not None
    assert result.outs["network_name"] == "kind"
    assert result.outs["port"] == 5000
    assert result.outs["ip_address"] == "172.18.0.6"


def test_provider_diff_replaces_on_container_or_network_change() -> None:
    provider = ctlptl_registry_service._CtlptlRegistryNetworkAddressProvider()

    container_diff = provider.diff(
        "custom-registry:kind",
        {"container_name": "old", "network_name": "kind", "port": 5000},
        {"container_name": "new", "network_name": "kind", "port": 5000},
    )
    network_diff = provider.diff(
        "custom-registry:kind",
        {
            "container_name": "custom-registry",
            "network_name": "kind",
            "port": 5000,
        },
        {
            "container_name": "custom-registry",
            "network_name": "other",
            "port": 5000,
        },
    )
    port_diff = provider.diff(
        "custom-registry:kind",
        {
            "container_name": "custom-registry",
            "network_name": "kind",
            "port": 5000,
        },
        {
            "container_name": "custom-registry",
            "network_name": "kind",
            "port": 5001,
        },
    )

    assert container_diff.replaces == ["container_name"]
    assert network_diff.replaces == ["network_name"]
    assert port_diff.changes is True
    assert port_diff.replaces == []