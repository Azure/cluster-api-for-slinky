# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "projects/awx/inventory/capi_slurm_inventory.py"
_SPEC = importlib.util.spec_from_file_location("capi_slurm_inventory", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
capi_slurm_inventory = importlib.util.module_from_spec(_SPEC)
sys.modules["capi_slurm_inventory"] = capi_slurm_inventory
_SPEC.loader.exec_module(capi_slurm_inventory)


def _machine(
    *,
    name: str,
    cluster_name: str,
    node_type: str | None,
    hostname: str,
    external_ip: str,
) -> dict[str, object]:
    labels = {"cluster.x-k8s.io/cluster-name": cluster_name}
    if node_type is not None:
        labels["slinky.slurm.net/node-type"] = node_type
    return {
        "metadata": {
            "name": name,
            "namespace": "default",
            "labels": labels,
        },
        "status": {
            "addresses": [
                {"type": "Hostname", "address": hostname},
                {"type": "ExternalIP", "address": external_ip},
            ],
            "nodeRef": {"name": hostname},
        },
    }


def test_inventory_from_machines_groups_slinky_nodes_by_cluster_and_role() -> None:
    inventory = capi_slurm_inventory.inventory_from_machines(
        [
            _machine(
                name="local-head-abc",
                cluster_name="local-workload",
                node_type="controller",
                hostname="local-head",
                external_ip="172.18.0.13",
            ),
            _machine(
                name="local-compute-abc",
                cluster_name="local-workload",
                node_type="compute",
                hostname="local-compute",
                external_ip="172.18.0.14",
            ),
            _machine(
                name="local-control-plane",
                cluster_name="local-workload",
                node_type=None,
                hostname="local-control-plane",
                external_ip="172.18.0.12",
            ),
        ]
    )

    assert inventory["management"] == {"hosts": ["localhost"]}
    assert inventory["controller_local_workload"] == {"hosts": ["local-head"]}
    assert inventory["compute_local_workload"] == {"hosts": ["local-compute"]}
    assert inventory["controller"] == {"children": ["controller_local_workload"]}
    assert inventory["compute"] == {"children": ["compute_local_workload"]}
    assert inventory["cluster_local_workload"] == {
        "children": ["compute_local_workload", "controller_local_workload"]
    }
    assert inventory["_meta"]["hostvars"]["local-compute"] == {
        "ansible_host": "172.18.0.14",
        "capi_cluster": "local-workload",
        "capi_machine": "local-compute-abc",
        "capi_namespace": "default",
        "node_type": "compute",
    }
    assert "local-control-plane" not in inventory["_meta"]["hostvars"]


def test_summary_from_machines_reports_discovered_hosts() -> None:
    summary = capi_slurm_inventory.summary_from_machines(
        [
            _machine(
                name="local-compute-abc",
                cluster_name="local-workload",
                node_type="compute",
                hostname="local-compute",
                external_ip="172.18.0.14",
            )
        ]
    )

    assert summary["clusters"] == ["local-workload"]
    assert summary["hosts"] == ["local-compute"]
    assert summary["host_count"] == 1
    assert "compute" in summary["groups"]
