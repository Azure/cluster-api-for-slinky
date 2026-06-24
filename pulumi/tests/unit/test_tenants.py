from __future__ import annotations

import pytest

from stacks.workload_cluster.tenants import (
    ObjectMeta,
    WorkloadClusterSpec,
    parse_tenants_spec,
)


def test_parse_tenants_spec_defaults_to_local_cluster() -> None:
    spec = parse_tenants_spec(None)

    assert spec.workload_clusters == (
        WorkloadClusterSpec(metadata=ObjectMeta(name="local"), class_name="local"),
    )


def test_parse_tenants_spec_accepts_kubernetes_flavored_shape() -> None:
    spec = parse_tenants_spec(
        {
            "workloadClusters": [
                {
                    "metadata": {
                        "name": "local-2",
                        "labels": {"slinky.slurm.net/environment": "local"},
                    },
                    "spec": {
                        "className": "local",
                        "parameters": {},
                    },
                },
                {
                    "metadata": {"name": "local"},
                    "spec": {"className": "local"},
                },
            ],
        }
    )

    assert spec.workload_clusters == (
        WorkloadClusterSpec(metadata=ObjectMeta(name="local"), class_name="local"),
        WorkloadClusterSpec(
            metadata=ObjectMeta(
                name="local-2",
                labels={"slinky.slurm.net/environment": "local"},
            ),
            class_name="local",
        ),
    )


def test_parse_tenants_spec_rejects_duplicate_cluster_names() -> None:
    with pytest.raises(ValueError, match="duplicate names: local"):
        parse_tenants_spec(
            {
                "workloadClusters": [
                    {
                        "metadata": {"name": "local"},
                        "spec": {"className": "local"},
                    },
                    {
                        "metadata": {"name": "local"},
                        "spec": {"className": "local"},
                    },
                ],
            }
        )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"workloadClusters": []},
        {"workloadClusters": [{"metadata": {"name": "local"}}]},
        {
            "workloadClusters": [
                {"metadata": {"name": "not_valid"}, "spec": {"className": "local"}}
            ]
        },
        {
            "workloadClusters": [
                {"metadata": {"name": "local"}, "spec": {"className": "not_valid"}}
            ]
        },
    ],
)
def test_parse_tenants_spec_rejects_invalid_shape(value: object) -> None:
    with pytest.raises(ValueError):
        parse_tenants_spec(value)