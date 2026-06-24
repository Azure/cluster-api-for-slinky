"""Tenants aggregate for the ``azure`` outer env.

This component provisions a **single AKS managed workload cluster** via
:class:`stacks.workload_cluster.workload_cluster_class_aks.AKSWorkloadClusterClass`.

:class:`pko._init_stack_azure.InitStackAzure` instantiates this alongside
``ControlPlaneAzure`` and threads in (a) the parsed
:class:`AzureWorkloadSpec` describing where/how big the cluster should be,
(b) the subscription ID from the Phase 1 identity spec, and (c) the
``AzureClusterIdentity`` name + namespace outputs so the AKS control plane's
``identityRef`` points back at the Phase 1 identity.

This is the Azure analogue of :mod:`stacks.workload_cluster.tenants_local`,
but deliberately simpler: there is a single hardcoded AKS tenant rather than
a config-driven fan-out. A multi-tenant fan-out (and a self-managed cluster
class) can be added later by mirroring ``tenants_local``'s importlib
dispatch; for now a single managed cluster keeps the first working CAPZ
workload increment small.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import pulumi
from pulumi import ResourceOptions

try:
    from .workload_cluster_class_aks import AKSWorkloadClusterClass
except ImportError:  # pragma: no cover - exercised only outside the package
    from workload_cluster_class_aks import AKSWorkloadClusterClass


AZURE_WORKLOAD_CHILD_CONFIG_KEY = "azureWorkload"
_CONFIG_LOCATION = "location"
_CONFIG_RESOURCE_GROUP = "resourceGroup"
_CONFIG_ADDITIONAL_TAGS = "additionalTags"
_CONFIG_AKS = "aks"
_CONFIG_AKS_KUBERNETES_VERSION = "kubernetesVersion"
_CONFIG_AKS_NODE_SKU = "nodeSku"
_CONFIG_AKS_NODE_COUNT = "nodeCount"

# AKS defaults. Every one is overridable via config so that an operational
# change (a region dropping support for a Kubernetes patch, a SKU quota
# change) is a config edit, not a code change.
_DEFAULT_AKS_KUBERNETES_VERSION = "v1.33.12"
_DEFAULT_AKS_NODE_SKU = "Standard_D2as_v5"
_DEFAULT_AKS_NODE_COUNT = 1


@dataclass(frozen=True)
class AzureWorkloadSpec:
    """Where and how big the AKS workload cluster should be."""

    location: str
    resource_group: str
    additional_tags: Mapping[str, str] = field(default_factory=dict)
    kubernetes_version: str = _DEFAULT_AKS_KUBERNETES_VERSION
    node_sku: str = _DEFAULT_AKS_NODE_SKU
    node_count: int = _DEFAULT_AKS_NODE_COUNT


def _require_non_empty_str(field_path: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_path} must be a non-empty string")
    return value


def _require_positive_int(field_path: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_path} must be an integer >= 1")
    return value


def _require_str_map(field_path: str, value: object) -> dict[str, str]:
    """Validate a ``{str: str}`` map from hand-edited config."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object of string tags")
    tags: dict[str, str] = {}
    for key, tag_value in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_path} keys must be non-empty strings")
        if not isinstance(tag_value, str):
            raise ValueError(f"{field_path}.{key} must be a string")
        tags[key] = tag_value
    return tags


def parse_azure_workload_spec(value: object | None) -> AzureWorkloadSpec:
    """Parse the ``childConfig.azureWorkload`` map into a typed spec."""
    if value is None:
        raise ValueError(
            "missing required Azure workload-cluster config under "
            f"{AZURE_WORKLOAD_CHILD_CONFIG_KEY!r}; the outer stack_azure.py "
            "must pass build_azure_workload_child_config(...) into "
            "PKOBootstrap(config=...)"
        )
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{AZURE_WORKLOAD_CHILD_CONFIG_KEY!r} config must be an object; "
            f"got {type(value).__name__}"
        )

    location = _require_non_empty_str(
        f"{AZURE_WORKLOAD_CHILD_CONFIG_KEY}.{_CONFIG_LOCATION}",
        value.get(_CONFIG_LOCATION),
    )
    resource_group = _require_non_empty_str(
        f"{AZURE_WORKLOAD_CHILD_CONFIG_KEY}.{_CONFIG_RESOURCE_GROUP}",
        value.get(_CONFIG_RESOURCE_GROUP),
    )

    tags_value = value.get(_CONFIG_ADDITIONAL_TAGS)
    additional_tags = (
        {}
        if tags_value is None
        else _require_str_map(
            f"{AZURE_WORKLOAD_CHILD_CONFIG_KEY}.{_CONFIG_ADDITIONAL_TAGS}",
            tags_value,
        )
    )

    aks_value = value.get(_CONFIG_AKS)
    if aks_value is None:
        aks_fields: Mapping[str, object] = {}
    elif isinstance(aks_value, Mapping):
        aks_fields = aks_value
    else:
        raise ValueError(
            f"{AZURE_WORKLOAD_CHILD_CONFIG_KEY}.{_CONFIG_AKS} must be an "
            f"object; got {type(aks_value).__name__}"
        )

    aks_path = f"{AZURE_WORKLOAD_CHILD_CONFIG_KEY}.{_CONFIG_AKS}"
    version_value = aks_fields.get(_CONFIG_AKS_KUBERNETES_VERSION)
    kubernetes_version = (
        _DEFAULT_AKS_KUBERNETES_VERSION
        if version_value is None
        else _require_non_empty_str(
            f"{aks_path}.{_CONFIG_AKS_KUBERNETES_VERSION}", version_value
        )
    )
    sku_value = aks_fields.get(_CONFIG_AKS_NODE_SKU)
    node_sku = (
        _DEFAULT_AKS_NODE_SKU
        if sku_value is None
        else _require_non_empty_str(
            f"{aks_path}.{_CONFIG_AKS_NODE_SKU}", sku_value
        )
    )
    count_value = aks_fields.get(_CONFIG_AKS_NODE_COUNT)
    node_count = (
        _DEFAULT_AKS_NODE_COUNT
        if count_value is None
        else _require_positive_int(
            f"{aks_path}.{_CONFIG_AKS_NODE_COUNT}", count_value
        )
    )

    return AzureWorkloadSpec(
        location=location,
        resource_group=resource_group,
        additional_tags=additional_tags,
        kubernetes_version=kubernetes_version,
        node_sku=node_sku,
        node_count=node_count,
    )


def build_azure_workload_child_config(
    *,
    location: str,
    resource_group: str,
    additional_tags: Mapping[str, str] | None = None,
    aks_kubernetes_version: str | None = None,
    aks_node_sku: str | None = None,
    aks_node_count: int | None = None,
) -> dict[str, object]:
    """Build the ``{azureWorkload: {...}}`` dict for PKOBootstrap(config=...)."""
    child: dict[str, object] = {
        _CONFIG_LOCATION: location,
        _CONFIG_RESOURCE_GROUP: resource_group,
    }
    if additional_tags:
        child[_CONFIG_ADDITIONAL_TAGS] = dict(additional_tags)
    aks: dict[str, object] = {}
    if aks_kubernetes_version is not None:
        aks[_CONFIG_AKS_KUBERNETES_VERSION] = aks_kubernetes_version
    if aks_node_sku is not None:
        aks[_CONFIG_AKS_NODE_SKU] = aks_node_sku
    if aks_node_count is not None:
        aks[_CONFIG_AKS_NODE_COUNT] = aks_node_count
    if aks:
        child[_CONFIG_AKS] = aks
    return {AZURE_WORKLOAD_CHILD_CONFIG_KEY: child}


# Single hardcoded tenant for this increment. Used as the CR/AKS cluster
# name prefix (``caps-aks``, ``caps-aks-pool0``).
_AKS_INSTANCE_NAME = "caps-aks"


class TenantsAzure(pulumi.ComponentResource):
    """Instantiate the ``azure`` workload-cluster tenant(s).

    Args:
        name: Pulumi resource name.
        workload_spec: parsed workload-cluster placement/sizing spec.
        subscription_id: Azure subscription ID (from the Phase 1 identity
            spec; the AKS control plane is billed to it).
        identity_name / identity_namespace: name + namespace of the
            ``AzureClusterIdentity`` created by ``ControlPlaneAzure``,
            referenced by the AKS control plane's ``identityRef``.
        opts: standard ``pulumi.ResourceOptions``.

    Outputs:
        workload_clusters: one-element list describing the AKS cluster. The
            list shape (not a bare dict) is preserved so the init stack and
            outer ``stack_azure.py`` consume it the same way as
            :class:`stacks.workload_cluster.tenants_local.TenantsLocal`.
    """

    workload_clusters: list[dict[str, object]]

    def __init__(
        self,
        name: str,
        *,
        workload_spec: AzureWorkloadSpec,
        subscription_id: str,
        identity_name: pulumi.Input[str],
        identity_namespace: pulumi.Input[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:TenantsAzure",
            name,
            props={},
            opts=opts,
        )

        aks = AKSWorkloadClusterClass(
            _AKS_INSTANCE_NAME,
            instance=_AKS_INSTANCE_NAME,
            workload_spec=workload_spec,
            subscription_id=subscription_id,
            identity_name=identity_name,
            identity_namespace=identity_namespace,
            opts=ResourceOptions(parent=self),
        )

        # List shape (not a bare dict) mirrors ``TenantsLocal.workload_clusters``
        # so the init stack and outer ``stack_azure.py`` consume both the same
        # way. A single hardcoded tenant today; a config-driven fan-out lands
        # later.
        self.workload_clusters = [
            {
                "class": aks.cluster_class,
                "instance": aks.cluster_instance,
                "cluster_name": aks.cluster_name,
                "control_plane_name": aks.control_plane_name,
                "machine_pool_name": aks.machine_pool_name,
                "machine_pool_names": aks.machine_pool_names,
                "control_plane_ready": aks.control_plane_ready,
                "keda_namespace": aks.keda_namespace,
                "keda_scaled_object_names": aks.keda_scaled_object_names,
                "keda_status": aks.keda_status,
                "prometheus_namespace": aks.prometheus_namespace,
                "prometheus_status": aks.prometheus_status,
                "workload_cluster_ready": aks.workload_cluster_ready,
                "todo": aks.todo,
            }
        ]

        self.register_outputs(
            {"workload_clusters": self.workload_clusters}
        )
