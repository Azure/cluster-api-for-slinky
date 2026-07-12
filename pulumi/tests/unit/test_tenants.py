from __future__ import annotations

import pytest

from stacks.workload_cluster.workload_cluster_class_azure_byo import (
    AzureBYOWorkloadClusterConfig,
)
from stacks.workload_cluster.workload_cluster_class_local import LocalWorkloadClusterConfig
from stacks.workload_cluster.tenants import (
    Tenants,
    TenantsConfig,
    WorkloadClusterContext,
)


def test_tenants_config_defaults_to_local_cluster() -> None:
    spec = TenantsConfig()

    assert spec.workload_clusters == {
        "local": LocalWorkloadClusterConfig(),
    }


def test_workload_cluster_context_is_plain_pydantic_model() -> None:
    context = WorkloadClusterContext(
        identity_name="cluster-identity",
        identity_namespace="default",
        azure_client_id="11111111-1111-1111-1111-111111111111",
        azure_tenant_id="22222222-2222-2222-2222-222222222222",
    )

    assert context.model_dump() == {
        "identity_name": "cluster-identity",
        "identity_namespace": "default",
        "azure_client_id": "11111111-1111-1111-1111-111111111111",
        "azure_tenant_id": "22222222-2222-2222-2222-222222222222",
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


def test_tenants_config_accepts_azure_byo_class() -> None:
    spec = TenantsConfig.model_validate(
        {
            "workloadClusters": {
                "caps-self": {
                    "className": "azure-byo",
                    "parameters": {
                        "subscriptionId": "44444444-4444-4444-4444-444444444444",
                        "location": "southcentralus",
                    },
                }
            }
        }
    )

    assert isinstance(
        spec.workload_clusters["caps-self"], AzureBYOWorkloadClusterConfig
    )
    assert spec.to_config()["workloadClusters"]["caps-self"]["className"] == (
        "azure-byo"
    )


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