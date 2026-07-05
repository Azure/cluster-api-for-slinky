from __future__ import annotations

import pytest

from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig
from stacks.workload_cluster.tenants import (
    Tenants,
    WorkloadClusterContext,
    TenantsConfig,
)


def test_tenants_config_defaults_to_local_cluster() -> None:
    spec = TenantsConfig()

    assert spec.workload_clusters == {
        "local": LocalWorkloadClusterConfig(),
    }


def test_tenants_config_accepts_mapping_shape() -> None:
    spec = TenantsConfig.model_validate(
        {
            "workloadClusters": {
                "local-2": {
                    "className": "local",
                },
                "local": {
                    "className": "local",
                },
            },
        }
    )

    assert spec.workload_clusters == {
        "local": LocalWorkloadClusterConfig(),
        "local-2": LocalWorkloadClusterConfig(),
    }
    assert spec.to_config() == {
        "workloadClusters": {
            "local": {"className": "local"},
            "local-2": {"className": "local"},
        }
    }


def test_tenants_config_accepts_empty_mapping() -> None:
    spec = TenantsConfig.model_validate({"workloadClusters": {}})

    assert spec.workload_clusters == {}


@pytest.mark.parametrize(
    "value",
    [
        {"workloadClusters": []},
        {"workloadClusters": {"local": {}}},
        {"workloadClusters": {"not_valid": {"className": "local"}}},
        {"workloadClusters": {"local": {"className": "not_valid"}}},
        {"workloadClusters": {"local": {"className": "local", "parameters": {}}}},
    ],
)
def test_tenants_config_rejects_invalid_shape(value: object) -> None:
    with pytest.raises(ValueError):
        TenantsConfig.model_validate(value)


def test_tenants_rejects_unsupported_workload_cluster_class() -> None:
    with pytest.raises(ValueError, match="Input tag 'bogus'"):
        TenantsConfig.model_validate(
            {"workloadClusters": {"sample": {"className": "bogus"}}}
        )