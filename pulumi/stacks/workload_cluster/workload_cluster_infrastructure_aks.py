"""Azure AKS workload-cluster infrastructure."""

from __future__ import annotations

import base64
import re
from typing import Mapping, Sequence

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions
from pydantic import StrictBool

from lib.config import NonEmptyStr, PulumiConfigModel, StrictPositiveInt
from stacks.workload_cluster.workload_cluster_infrastructure import (
    CONTROLLER_NODE_TYPE,
    NODE_TYPE_LABEL,
    node_labels,
)


# Core CAPI types (Cluster, MachinePool) on v1beta1 to match the CAPZ v1.24.1
# AKS template; CAPZ managed types on the infrastructure v1beta1 surface.
_CAPI_API_VERSION = "cluster.x-k8s.io/v1beta1"
_INFRASTRUCTURE_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta1"

_CLUSTER_KIND = "Cluster"
_MACHINE_POOL_KIND = "MachinePool"
_AMCP_KIND = "AzureManagedControlPlane"
_AMC_KIND = "AzureManagedCluster"
_AMMP_KIND = "AzureManagedMachinePool"
_AZURE_CLUSTER_IDENTITY_KIND = "AzureClusterIdentity"

_NAMESPACE = "default"
_WORKLOAD_KUBECONFIG_SECRET_KEY = "value"
# ``Cluster.spec.clusterNetwork.services.cidrBlocks`` — matches the upstream
# AKS template. For a managed control plane this is largely informational
# (AKS manages the real service CIDR), but the CAPI contract wants it set.
_SERVICE_CIDR = "192.168.0.0/16"
# Azure CNI (VNet-integrated pod IPs). The other supported value is
# ``kubenet``; Azure CNI is the chosen default for this project.
_NETWORK_PLUGIN = "azure"
# AKS restricts Linux pool names to <=12 lowercase alphanumerics. The CR
# metadata.name may be longer, so keep the provider pool name separately.
_AKS_POOL_NAME_MAX_LENGTH = 12
_SYSTEM_NODE_POOL_MODE = "System"
_USER_NODE_POOL_MODE = "User"
_AKS_CONTROLLER_NODE_LABELS = {NODE_TYPE_LABEL: CONTROLLER_NODE_TYPE}

_SKIP_AWAIT_ANNOTATION = "pulumi.com/skipAwait"
_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"
_WAIT_FOR_STATUS_READY = "jsonpath={.status.ready}=true"
_AKS_CONTROL_PLANE_TIMEOUT = "60m"
_AKS_DELETE_TIMEOUT = "60m"
_AMCP_IMMUTABLE_DEFAULTED_FIELDS = ["spec.sshPublicKey"]

_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


class AKSNodePoolSpec(PulumiConfigModel):
    name: NonEmptyStr
    node_type: NonEmptyStr
    replicas: StrictPositiveInt
    controller: StrictBool = False
    autoscaling_bounds: tuple[StrictPositiveInt, StrictPositiveInt] | None = None


def _resource_name(instance: str, suffix: str | None = None) -> str:
    """Derive a DNS-label-safe CR name from the tenant instance.

    Mirrors the helper in :mod:`workload_cluster_infrastructure_local`. With
    ``suffix`` omitted, returns the sanitized instance; with a suffix, returns
    ``<instance>-<suffix>`` truncated to the 63-char DNS-label limit.
    """
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", instance.lower()).strip("-")
    if not normalized:
        raise ValueError("instance must contain at least one alphanumeric character")
    if suffix is None:
        return normalized[:_DNS_LABEL_MAX_LENGTH].rstrip("-")
    normalized = normalized[: _DNS_LABEL_MAX_LENGTH - len(suffix) - 1].rstrip("-")
    return f"{normalized}-{suffix}"


def _cluster_spec(
    *,
    control_plane_name: str,
    infrastructure_name: str,
) -> dict[str, object]:
    """``Cluster.spec`` wiring the CAPI envelope to the AKS control plane."""
    return {
        "clusterNetwork": {"services": {"cidrBlocks": [_SERVICE_CIDR]}},
        "controlPlaneRef": {
            "apiVersion": _INFRASTRUCTURE_API_VERSION,
            "kind": _AMCP_KIND,
            "name": control_plane_name,
        },
        "infrastructureRef": {
            "apiVersion": _INFRASTRUCTURE_API_VERSION,
            "kind": _AMC_KIND,
            "name": infrastructure_name,
        },
    }


def _azure_managed_control_plane_spec(
    *,
    identity_name: pulumi.Input[str],
    identity_namespace: pulumi.Input[str],
    location: str,
    resource_group: str,
    subscription_id: str,
    version: str,
    additional_tags: Mapping[str, str],
) -> dict[str, object]:
    """``AzureManagedControlPlane.spec`` — the AKS control plane definition.

    ``additional_tags`` lands on ``spec.additionalTags``, which CAPZ maps to
    the AKS cluster's tags. AKS propagates those to the auto-created node
    resource group (``MC_<rg>_<cluster>_<location>``) — the surface the org
    Azure Policy actually checks for the required ``Owner`` tag before the
    node VMSS may be created. This (not the per-machine-pool tag, which lands
    on the VMSS) is what satisfies the RG-scoped policy. Omitted from the spec
    when empty.
    """
    spec: dict[str, object] = {
        "identityRef": {
            "apiVersion": _INFRASTRUCTURE_API_VERSION,
            "kind": _AZURE_CLUSTER_IDENTITY_KIND,
            "name": identity_name,
            "namespace": identity_namespace,
        },
        "location": location,
        "resourceGroupName": resource_group,
        "subscriptionID": subscription_id,
        "version": version,
        "networkPlugin": _NETWORK_PLUGIN,
        # Empty string == no SSH key on AKS nodes. AKS does not require one;
        # matches the upstream template's ``${AZURE_SSH_PUBLIC_KEY_B64:=""}``.
        "sshPublicKey": "",
        # Enable the OIDC issuer so a later increment can adopt workload
        # identity without recreating the control plane.
        "oidcIssuerProfile": {"enabled": True},
    }
    if additional_tags:
        spec["additionalTags"] = dict(additional_tags)
    return spec


def _machine_pool_spec(
    *,
    cluster_name: str,
    pool_name: str,
    version: str,
    replicas: int,
) -> dict[str, object]:
    """``MachinePool.spec`` — the CAPI envelope for the system agent pool."""
    return {
        "clusterName": cluster_name,
        "replicas": replicas,
        "template": {
            "spec": {
                # AKS handles bootstrap; the CAPI contract still wants an
                # (empty) bootstrap reference, so pin an empty dataSecretName.
                "bootstrap": {"dataSecretName": ""},
                "clusterName": cluster_name,
                "infrastructureRef": {
                    "apiVersion": _INFRASTRUCTURE_API_VERSION,
                    "kind": _AMMP_KIND,
                    "name": pool_name,
                },
                "version": version,
            },
        },
    }


def _aks_pool_name(pool: AKSNodePoolSpec) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", pool.name.lower())
    if not normalized:
        raise ValueError("AKS worker pool name must contain at least one alphanumeric")
    if pool.controller:
        normalized = f"sys{normalized}"
    return normalized[:_AKS_POOL_NAME_MAX_LENGTH]


def _azure_managed_machine_pool_spec(
    *,
    mode: str,
    pool_name: str,
    sku: str,
    additional_tags: Mapping[str, str],
    node_labels: Mapping[str, str],
    autoscaling_bounds: tuple[int, int] | None = None,
) -> dict[str, object]:
    """``AzureManagedMachinePool.spec`` for one AKS managed node pool.

    ``additional_tags`` lands on ``spec.additionalTags`` and is stamped by
    CAPZ onto the agent pool's underlying VMSS. ``node_labels`` labels the AKS
    nodes so the shared workload deployments can use the same placement rules
    as the CAPD path.
    """
    spec: dict[str, object] = {
        "mode": mode,
        "name": pool_name,
        "sku": sku,
        "nodeLabels": dict(node_labels),
    }
    if additional_tags:
        spec["additionalTags"] = dict(additional_tags)
    if autoscaling_bounds is not None:
        min_count, max_count = autoscaling_bounds
        spec["scaling"] = {"minSize": min_count, "maxSize": max_count}
    return spec


def _decode_secret_data_value(data: Mapping[str, str], key: str) -> str:
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


class AKSWorkloadClusterInfrastructure(pulumi.ComponentResource):
    """Provision AKS managed infrastructure via the CAPZ managed CR set."""

    cluster_name: Output[str]
    control_plane_name: Output[str]
    machine_pool_name: Output[str]
    machine_pool_names: list[Output[str]]
    control_plane_ready: Output[bool]
    workload_kubeconfig: Output[str]
    workload_provider: k8s.Provider
    workload_kubeconfig_secret: k8s.core.v1.Secret

    def __init__(
        self,
        name: str,
        *,
        instance: str,
        subscription_id: str,
        identity_name: pulumi.Input[str],
        identity_namespace: pulumi.Input[str],
        location: str,
        resource_group: str,
        kubernetes_version: str,
        node_sku: str,
        node_pools: tuple[AKSNodePoolSpec, ...],
        additional_tags: Mapping[str, str] | None = None,
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:AKSWorkloadClusterInfrastructure",
            name,
            props={},
            opts=opts,
        )

        cluster_name = _resource_name(instance)

        def child_opts(
            depends_on: Sequence[pulumi.Input[pulumi.Resource]] | None = None,
        ) -> ResourceOptions:
            return ResourceOptions(
                parent=self, provider=provider, depends_on=depends_on
            )

        azure_managed_cluster = k8s.apiextensions.CustomResource(
            f"{name}-managed-cluster",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind=_AMC_KIND,
            metadata={"name": cluster_name, "namespace": _NAMESPACE},
            spec={},
            opts=child_opts(),
        )

        azure_managed_control_plane = k8s.apiextensions.CustomResource(
            f"{name}-control-plane",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind=_AMCP_KIND,
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                # CAPZ cannot make AMCP ready until at least one System AMMP exists.
                # The explicit ready patch below waits after machine pools are created.
                "annotations": {_SKIP_AWAIT_ANNOTATION: "true"},
            },
            spec=_azure_managed_control_plane_spec(
                identity_name=identity_name,
                identity_namespace=identity_namespace,
                location=location,
                resource_group=resource_group,
                subscription_id=subscription_id,
                version=kubernetes_version,
                additional_tags=dict(additional_tags or {}),
            ),
            opts=pulumi.ResourceOptions.merge(
                child_opts(depends_on=[azure_managed_cluster]),
                pulumi.ResourceOptions(
                    ignore_changes=_AMCP_IMMUTABLE_DEFAULTED_FIELDS,
                    custom_timeouts=pulumi.CustomTimeouts(
                        create=_AKS_CONTROL_PLANE_TIMEOUT,
                        update=_AKS_CONTROL_PLANE_TIMEOUT,
                        delete=_AKS_CONTROL_PLANE_TIMEOUT,
                    )
                ),
            ),
        )

        cluster = k8s.apiextensions.CustomResource(
            f"{name}-cluster",
            api_version=_CAPI_API_VERSION,
            kind=_CLUSTER_KIND,
            metadata={"name": cluster_name, "namespace": _NAMESPACE},
            spec=_cluster_spec(
                control_plane_name=cluster_name,
                infrastructure_name=cluster_name,
            ),
            opts=pulumi.ResourceOptions.merge(
                child_opts(depends_on=[azure_managed_control_plane]),
                pulumi.ResourceOptions(
                    custom_timeouts=pulumi.CustomTimeouts(delete=_AKS_DELETE_TIMEOUT)
                ),
            ),
        )

        machine_pools: list[k8s.apiextensions.CustomResource] = []
        machine_pool_names: list[Output[str]] = []
        for pool in node_pools:
            cr_pool_name = _resource_name(instance, pool.name)
            aks_pool_name = _aks_pool_name(pool)
            pool_mode = _SYSTEM_NODE_POOL_MODE if pool.controller else _USER_NODE_POOL_MODE
            delete_with_cluster_opts = (
                pulumi.ResourceOptions(deleted_with=cluster)
                if pool.controller
                else pulumi.ResourceOptions()
            )
            azure_managed_machine_pool = k8s.apiextensions.CustomResource(
                f"{name}-{pool.name}-managed-machine-pool",
                api_version=_INFRASTRUCTURE_API_VERSION,
                kind=_AMMP_KIND,
                metadata={"name": cr_pool_name, "namespace": _NAMESPACE},
                spec=_azure_managed_machine_pool_spec(
                    mode=pool_mode,
                    pool_name=aks_pool_name,
                    sku=node_sku,
                    additional_tags=dict(additional_tags or {}),
                    node_labels=node_labels(pool.node_type),
                    autoscaling_bounds=pool.autoscaling_bounds,
                ),
                opts=pulumi.ResourceOptions.merge(
                    pulumi.ResourceOptions.merge(
                        child_opts(depends_on=[azure_managed_control_plane]),
                        pulumi.ResourceOptions(
                            custom_timeouts=pulumi.CustomTimeouts(
                                delete=_AKS_DELETE_TIMEOUT
                            )
                        ),
                    ),
                    delete_with_cluster_opts,
                ),
            )

            machine_pool = k8s.apiextensions.CustomResource(
                f"{name}-{pool.name}-machine-pool",
                api_version=_CAPI_API_VERSION,
                kind=_MACHINE_POOL_KIND,
                metadata={"name": cr_pool_name, "namespace": _NAMESPACE},
                spec=_machine_pool_spec(
                    cluster_name=cluster_name,
                    pool_name=cr_pool_name,
                    version=kubernetes_version,
                    replicas=pool.replicas,
                ),
                opts=pulumi.ResourceOptions.merge(
                    child_opts(
                        depends_on=[
                            cluster,
                            azure_managed_control_plane,
                            azure_managed_machine_pool,
                        ]
                    ),
                    pulumi.ResourceOptions(
                        custom_timeouts=pulumi.CustomTimeouts(
                            delete=_AKS_DELETE_TIMEOUT
                        )
                    ),
                    delete_with_cluster_opts,
                ),
            )
            machine_pools.append(machine_pool)
            machine_pool_names.append(Output.from_input(cr_pool_name))

        azure_managed_control_plane_ready = k8s.apiextensions.CustomResourcePatch(
            f"{name}-control-plane-ready",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind=_AMCP_KIND,
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": {_WAIT_FOR_ANNOTATION: _WAIT_FOR_STATUS_READY},
            },
            opts=pulumi.ResourceOptions.merge(
                child_opts(depends_on=machine_pools),
                pulumi.ResourceOptions(
                    custom_timeouts=pulumi.CustomTimeouts(
                        create=_AKS_CONTROL_PLANE_TIMEOUT,
                        update=_AKS_CONTROL_PLANE_TIMEOUT,
                        delete=_AKS_CONTROL_PLANE_TIMEOUT,
                    )
                ),
            ),
        )

        workload_kubeconfig_secret = k8s.core.v1.Secret.get(
            "workload-kubeconfig-secret",
            id=f"{_NAMESPACE}/{cluster_name}-kubeconfig",
            opts=child_opts(depends_on=[azure_managed_control_plane_ready]),
        )
        workload_kubeconfig = pulumi.Output.secret(
            workload_kubeconfig_secret.data.apply(
                lambda data: _decode_secret_data_value(
                    data,
                    _WORKLOAD_KUBECONFIG_SECRET_KEY,
                )
            )
        )
        workload_provider = k8s.Provider(
            "workload-k8s",
            kubeconfig=workload_kubeconfig,
            upsert_existing_objects=True,
            opts=ResourceOptions(parent=self, depends_on=[workload_kubeconfig_secret]),
        )

        self.cluster_name = Output.from_input(cluster_name)
        self.control_plane_name = Output.from_input(cluster_name)
        self.machine_pool_names = machine_pool_names
        self.machine_pool_name = machine_pool_names[0]
        self.control_plane_ready = azure_managed_control_plane_ready.id.apply(
            lambda _: True
        )
        self.workload_kubeconfig = workload_kubeconfig
        self.workload_provider = workload_provider
        self.workload_kubeconfig_secret = workload_kubeconfig_secret

        self.register_outputs(
            {
                "cluster_name": self.cluster_name,
                "control_plane_name": self.control_plane_name,
                "machine_pool_name": self.machine_pool_name,
                "machine_pool_names": self.machine_pool_names,
                "control_plane_ready": self.control_plane_ready,
            }
        )
