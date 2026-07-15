"""Shared workload-cluster addon value rendering tests."""

from __future__ import annotations

from stacks.workload_cluster.workload_cluster_addons import (
    _azure_cloud_provider_values,
    _calico_vxlan_values,
)


def test_azure_cloud_provider_values_cover_bootstrap_taints() -> None:
    values = _azure_cloud_provider_values(
        cluster_name="caps-self",
        pod_cidr="192.168.0.0/16",
    )

    assert values["infra"] == {"clusterName": "caps-self"}
    ccm = values["cloudControllerManager"]
    assert ccm["clusterCIDR"] == "192.168.0.0/16"
    assert ccm["configureCloudRoutes"] == "true"
    assert {
        toleration["key"]
        for toleration in ccm["tolerations"]
    } >= {
        "node-role.kubernetes.io/control-plane",
        "node.cloudprovider.kubernetes.io/uninitialized",
        "node.kubernetes.io/not-ready",
    }


def test_calico_uses_always_on_vxlan() -> None:
    values = _calico_vxlan_values(pod_cidr="192.168.0.0/16")

    pool = values["installation"]["calicoNetwork"]["ipPools"][0]
    assert pool == {
        "name": "default-ipv4-ippool",
        "blockSize": 26,
        "cidr": "192.168.0.0/16",
        "encapsulation": "VXLAN",
        "natOutgoing": "Enabled",
        "nodeSelector": "all()",
    }
