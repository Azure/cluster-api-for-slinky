"""Unit tests for the WorkloadClusterAzureAKS CR spec helpers + name derivation.

The full Pulumi resource graph (the five ``CustomResource`` objects and the
``waitFor`` gating annotation) is not rendered here — that needs a Pulumi
runtime and the asserted bits are constant. The testable surface is the set
of pure spec-builder functions and the DNS-label name derivation.
"""

from __future__ import annotations

import pytest

from stacks.workload_cluster.workload_cluster_azure_aks import (
    _AMC_KIND,
    _AMCP_KIND,
    _AMMP_KIND,
    _AZURE_CLUSTER_IDENTITY_KIND,
    _CAPI_API_VERSION,
    _INFRASTRUCTURE_API_VERSION,
    _NETWORK_PLUGIN,
    _NODE_POOL_MODE,
    _NODE_POOL_NAME,
    _SERVICE_CIDR,
    _azure_managed_control_plane_spec,
    _azure_managed_machine_pool_spec,
    _cluster_spec,
    _machine_pool_spec,
    _resource_name,
)


def test_resource_name_sanitizes_and_suffixes() -> None:
    assert _resource_name("caps-aks") == "caps-aks"
    assert _resource_name("caps-aks", _NODE_POOL_NAME) == "caps-aks-pool0"
    # Uppercase + underscores collapse to a DNS label.
    assert _resource_name("Caps_AKS") == "caps-aks"


def test_resource_name_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one alphanumeric"):
        _resource_name("---")


def test_api_versions_match_capz_aks_template() -> None:
    # The CAPZ v1.23.2 AKS template pairs core CAPI v1beta1 with the infra
    # v1beta1 surface. Pin both so a CAPI/CAPZ bump that drops v1beta1 fails
    # loudly in this test rather than silently at reconcile time.
    assert _CAPI_API_VERSION == "cluster.x-k8s.io/v1beta1"
    assert _INFRASTRUCTURE_API_VERSION == "infrastructure.cluster.x-k8s.io/v1beta1"


def test_cluster_spec_wires_managed_refs() -> None:
    spec = _cluster_spec(
        control_plane_name="caps-aks",
        infrastructure_name="caps-aks",
    )

    assert spec["clusterNetwork"] == {"services": {"cidrBlocks": [_SERVICE_CIDR]}}
    assert spec["controlPlaneRef"] == {
        "apiVersion": _INFRASTRUCTURE_API_VERSION,
        "kind": _AMCP_KIND,
        "name": "caps-aks",
    }
    assert spec["infrastructureRef"] == {
        "apiVersion": _INFRASTRUCTURE_API_VERSION,
        "kind": _AMC_KIND,
        "name": "caps-aks",
    }


def test_amcp_spec_carries_identity_and_placement() -> None:
    spec = _azure_managed_control_plane_spec(
        identity_name="cluster-identity",
        identity_namespace="default",
        location="westus2",
        resource_group="rg-capz-mi-dev2",
        subscription_id="d2c9544f-4329-4642-b73d-020e7fef844f",
        version="v1.30.6",
        additional_tags={},
    )

    assert spec["identityRef"] == {
        "apiVersion": _INFRASTRUCTURE_API_VERSION,
        "kind": _AZURE_CLUSTER_IDENTITY_KIND,
        "name": "cluster-identity",
        "namespace": "default",
    }
    assert spec["location"] == "westus2"
    assert spec["resourceGroupName"] == "rg-capz-mi-dev2"
    assert spec["subscriptionID"] == "d2c9544f-4329-4642-b73d-020e7fef844f"
    assert spec["version"] == "v1.30.6"
    assert spec["networkPlugin"] == _NETWORK_PLUGIN
    assert spec["sshPublicKey"] == ""
    assert spec["oidcIssuerProfile"] == {"enabled": True}
    # No tags supplied => no additionalTags key.
    assert "additionalTags" not in spec


def test_amcp_spec_stamps_additional_tags_for_node_rg_policy() -> None:
    # Control-plane additionalTags is the lever that satisfies the RG-scoped
    # Owner policy: AKS propagates the cluster's tags to the node resource
    # group, which is the surface the policy checks.
    spec = _azure_managed_control_plane_spec(
        identity_name="cluster-identity",
        identity_namespace="default",
        location="westus2",
        resource_group="rg-capz-mi-dev2",
        subscription_id="d2c9544f-4329-4642-b73d-020e7fef844f",
        version="v1.33.12",
        additional_tags={"Owner": "t-hernandezc"},
    )

    assert spec["additionalTags"] == {"Owner": "t-hernandezc"}


def test_machine_pool_spec_references_ammp_and_version() -> None:
    spec = _machine_pool_spec(
        cluster_name="caps-aks",
        pool_name="caps-aks-pool0",
        version="v1.30.6",
        replicas=2,
    )

    assert spec["clusterName"] == "caps-aks"
    assert spec["replicas"] == 2
    # Compare the whole template subtree in one assertion to avoid chained
    # subscripting into ``object``-typed values.
    assert spec["template"] == {
        "spec": {
            "bootstrap": {"dataSecretName": ""},
            "clusterName": "caps-aks",
            "infrastructureRef": {
                "apiVersion": _INFRASTRUCTURE_API_VERSION,
                "kind": _AMMP_KIND,
                "name": "caps-aks-pool0",
            },
            "version": "v1.30.6",
        },
    }


def test_ammp_spec_is_system_pool() -> None:
    spec = _azure_managed_machine_pool_spec(
        sku="Standard_D2s_v3", additional_tags={}
    )

    assert spec == {
        "mode": _NODE_POOL_MODE,
        "name": _NODE_POOL_NAME,
        "sku": "Standard_D2s_v3",
    }


def test_ammp_spec_stamps_additional_tags() -> None:
    # The per-pool additionalTags surface is how the Owner-tag Azure Policy is
    # satisfied; CAPZ stamps these onto the agent pool's VMSS.
    spec = _azure_managed_machine_pool_spec(
        sku="Standard_D2s_v3",
        additional_tags={"Owner": "t-hernandezc"},
    )

    assert spec == {
        "mode": _NODE_POOL_MODE,
        "name": _NODE_POOL_NAME,
        "sku": "Standard_D2s_v3",
        "additionalTags": {"Owner": "t-hernandezc"},
    }
