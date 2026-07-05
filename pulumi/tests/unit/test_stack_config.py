"""Unit tests for outer stack config routing."""

from __future__ import annotations

from stack import _azure_workload_requested, _with_local_registry_config
from stacks.init.init_stack import InitStackConfig
from stacks.workload_cluster.registry_setting import LocalPortRegistrySetting
from stacks.workload_cluster.tenants import TenantsConfig
from stacks.workload_cluster.workload_cluster_class_aks import (
    AKSWorkloadClusterConfig,
    AzureWorkloadSpec,
)
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig


class _Config:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get_object(self, key: str) -> object | None:
        value = self._values.get(key)
        return value if not isinstance(value, bool | str) else None


def test_empty_config_does_not_request_azure() -> None:
    assert not _azure_workload_requested(_Config({}))


def test_azure_object_config_requests_azure() -> None:
    assert _azure_workload_requested(
        _Config({"azure": {"additionalTags": {"owner": "platform"}}})
    )


def test_local_registry_config_is_applied_to_local_workload_clusters_only() -> None:
    config = InitStackConfig(
        tenants=TenantsConfig(
            workload_clusters={
                "local": LocalWorkloadClusterConfig(),
                "caps-aks": AKSWorkloadClusterConfig(
                    parameters=AzureWorkloadSpec(
                        location="westus2",
                        resource_group="rg-capz-mi-dev2",
                    )
                ),
            }
        )
    )

    updated = _with_local_registry_config(
        config,
        LocalPortRegistrySetting(port=5002),
    )

    assert updated.tenants.to_config() == {
        "workloadClusters": {
            "local": {
                "className": "local",
                "registry": {"kind": "local-port", "port": 5002},
            },
            "caps-aks": {
                "className": "aks",
                "parameters": {
                    "location": "westus2",
                    "resourceGroup": "rg-capz-mi-dev2",
                },
            },
        }
    }