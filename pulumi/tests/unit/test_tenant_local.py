from __future__ import annotations

import pytest

from stacks.workload_cluster.tenant_local import (
    WorkloadClusterSpec,
    parse_tenant_local_spec,
)


def test_parse_tenant_local_spec_defaults_to_local_cluster() -> None:
    spec = parse_tenant_local_spec(None)

    assert spec.workload_clusters == (
        WorkloadClusterSpec(name="local", cluster_class="local"),
    )


def test_parse_tenant_local_spec_accepts_workload_clusters_shape() -> None:
    spec = parse_tenant_local_spec(
        {
            "workloadClusters": [
                {"name": "local", "class": "local"},
                {"name": "local-2", "class": "local"},
            ],
        }
    )

    assert spec.workload_clusters == (
        WorkloadClusterSpec(name="local", cluster_class="local"),
        WorkloadClusterSpec(name="local-2", cluster_class="local"),
    )


def test_parse_tenant_local_spec_rejects_duplicate_cluster_names() -> None:
    with pytest.raises(ValueError, match="duplicate names: local"):
        parse_tenant_local_spec(
            {
                "workloadClusters": [
                    {"name": "local", "class": "local"},
                    {"name": "local", "class": "local"},
                ],
            }
        )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"workloadClusters": []},
        {"workloadClusters": [{"name": "local"}]},
        {"workloadClusters": [{"name": "not_valid", "class": "local"}]},
        {"workloadClusters": [{"name": "local", "class": "not_valid"}]},
    ],
)
def test_parse_tenant_local_spec_rejects_invalid_shape(value: object) -> None:
    with pytest.raises(ValueError):
        parse_tenant_local_spec(value)