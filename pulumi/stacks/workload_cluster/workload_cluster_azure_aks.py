"""Azure AKS (managed) workload-cluster component + config contract.

Phase 2 of the CAPZ migration. Where Phase 1 stopped at the identity
foundation (``AzureClusterIdentity`` + IMDS preflight, no workload
clusters), this module provisions a **single AKS managed cluster** via the
CAPZ "managed" CR set. It is the Azure analogue of
:mod:`stacks.workload_cluster.workload_cluster_local_local` (the CAPD
path), but deliberately much smaller because AKS owns most of the day-1
concerns the local path has to install by hand.

The five CRs (all in the ``default`` namespace)
----------------------------------------------
Modeled on the upstream ``cluster-template-aks.yaml`` shipped with
CAPZ v1.23.2 (the release this repo pins alongside CAPI v1.12.8):

1. ``Cluster`` (``cluster.x-k8s.io/v1beta1``) — the CAPI envelope. Its
   ``controlPlaneRef`` points at the ``AzureManagedControlPlane`` and its
   ``infrastructureRef`` at the ``AzureManagedCluster``.
2. ``AzureManagedControlPlane`` (AMCP, ``infrastructure.cluster.x-k8s.io/v1beta1``)
   — the AKS control plane itself: location, resource group, subscription,
   Kubernetes version, network plugin, and the ``identityRef`` back to the
   Phase 1 ``AzureClusterIdentity``. This is the CR that actually drives an
   ``az aks create`` under the hood (reconciled by CAPZ).
3. ``AzureManagedCluster`` (AMC, same API) — a thin infrastructure marker
   with an empty spec; CAPZ populates its ``controlPlaneEndpoint`` once the
   AMCP is up. Required by the CAPI contract (every ``Cluster`` needs an
   ``infrastructureRef``).
4. ``MachinePool`` (``cluster.x-k8s.io/v1beta1``) — the CAPI node-group
   envelope for the system node pool.
5. ``AzureManagedMachinePool`` (AMMP, infra API) — the AKS agent pool
   (``mode: System``, one pool named ``pool0``). AKS requires at least one
   System-mode pool, so ``pool0`` is it.

Why this is so much smaller than the CAPD path
----------------------------------------------
AKS is a *managed* control plane: Microsoft runs the API server, etcd, and
scheduler, and AKS installs its own CNI, CoreDNS, and storage drivers. So
unlike :mod:`workload_cluster_local_local`, this module installs **no**
Calico, **no** cert-manager-on-workload, **no** local-path storage, and
does no kubeadm wiring. Day-2 concerns (retrieving the workload kubeconfig,
landing Slurm / Slinky) are intentionally out of scope for this increment
and land later, mirroring how the local path grew incrementally.

API version note
----------------
The ``Cluster`` and ``MachinePool`` CRs use ``cluster.x-k8s.io/v1beta1``
here, NOT the ``v1beta2`` the local CAPD module uses. This matches the
CAPZ v1.23.2 AKS template verbatim: CAPZ's managed types
(``AzureManagedControlPlane`` etc.) are still served on the
``infrastructure.cluster.x-k8s.io/v1beta1`` surface, and the upstream
template pairs them with ``v1beta1`` core CRs. CAPI v1.12.8 still serves
``v1beta1`` for backward compatibility, so the pairing is valid. Pin
explicitly so a future CAPI/CAPZ bump that drops ``v1beta1`` fails loudly
here instead of silently.

Readiness gate
--------------
The AMCP carries ``pulumi.com/waitFor=condition=Ready``. AKS provisioning
takes several minutes; the annotation makes ``pulumi up`` block on the
AMCP's ``Ready`` condition so downstream consumers don't treat the cluster
as usable before AKS finishes. Crucially, the AMCP is created **last** (it
depends on the ``Cluster`` and ``MachinePool``): an AMCP can only reach
``Ready`` once its owning ``Cluster`` exists (CAPI wires the ownerRef) and a
System-mode agent pool is present, so creating it last avoids a
blocking-create deadlock against resources that don't exist yet.

childConfig.azureWorkload wire contract
---------------------------------------
This module also owns a small piece of the init-stack config contract: the
``childConfig.azureWorkload`` block that the outer ``stack_azure.py`` writes
and :class:`pko._init_stack_azure.InitStackAzure` reads. It is kept separate
from the ``childConfig.azure`` identity block (owned by
:mod:`stacks.control_plane.control_plane_azure`) so control-plane identity
concerns and workload-cluster sizing concerns don't bleed into one
dataclass. :func:`build_azure_workload_child_config` (writer) and
:func:`parse_azure_workload_spec` (reader) are symmetric so adding a field
touches both at once. The subscription ID is NOT duplicated here — it lives
in the identity block and is threaded in by ``InitStackAzure``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions


# ---------------------------------------------------------------------------
# childConfig.azureWorkload wire contract
# ---------------------------------------------------------------------------

# Top-level key under ``childConfig`` for the workload-cluster sizing/placement
# config. Distinct from CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY ("azure"), which
# carries the UAMI identity. Keeping them in separate top-level keys lets the
# control-plane and workload-cluster contracts evolve independently.
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
#
# IMPORTANT — verify the Kubernetes version before relying on the default.
# AKS enforces a support window and rejects ``az aks create`` for versions
# outside it. Confirm the pinned version is currently offered in the target
# region with::
#
#     az aks get-versions --location <region> --output table
#
# and override via ``ca4s-infra:aksKubernetesVersion`` if it is not. CAPZ
# surfaces an unsupported version as an AMCP reconcile error, which the
# ``waitFor=condition=Ready`` gate turns into a hard ``pulumi up`` failure.
#
# Pinned to v1.33.12 (verified 2026-06-16 as ``KubernetesOfficial`` +
# ``AKSLongTermSupport`` in westus2 via ``az aks get-versions``). Note the
# ``v`` prefix: CAPZ's ``AzureManagedControlPlane.spec.version`` follows the
# CAPI convention and wants it, even though ``az`` prints versions without.
#
# Node SKU: ``Standard_D2as_v5`` (AMD, 2 vCPU / 8 GiB). ``Standard_D2s_v3``
# was the original default but hit a transient ``SkuNotAvailable`` /
# "Capacity Restrictions" allocation failure in westus2 (2026-06-17); the
# AMD v5 family has broader capacity there. Verify availability for a region
# with ``az vm list-skus -l <region> --size <prefix> -o table`` and override
# via ``ca4s-infra:aksNodeSku`` if the chosen size is constrained.
_DEFAULT_AKS_KUBERNETES_VERSION = "v1.33.12"
_DEFAULT_AKS_NODE_SKU = "Standard_D2as_v5"
_DEFAULT_AKS_NODE_COUNT = 1


@dataclass(frozen=True)
class AzureWorkloadSpec:
    """Where and how big the AKS workload cluster should be.

    * ``location``       — Azure region for the AKS cluster (e.g.
      ``westus2``). Required; no safe default for "where to spend money".
    * ``resource_group`` — the resource group CAPZ provisions the AKS
      cluster into. May be an existing RG; AKS additionally auto-creates a
      *node* resource group named ``MC_<rg>_<cluster>_<location>`` for the
      VMSS-backed nodes.
    * ``additional_tags`` — extra Azure tags stamped onto EACH agent pool's
      VMSS via ``AzureManagedMachinePool.spec.additionalTags``. Required in
      this environment to satisfy the org Azure Policy that demands an
      ``Owner`` tag on the VMs (AKS does not reliably propagate
      cluster-level tags to the node VMSS, so the tag must be set per
      machine pool). Defaults to empty; supplied via config.
    * ``kubernetes_version`` / ``node_sku`` / ``node_count`` — system node
      pool sizing. Defaulted (see module constants) but overridable.
    """

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
    # ``bool`` is a subclass of ``int`` in Python; reject it explicitly so a
    # stray ``true``/``false`` in hand-edited config can't masquerade as a
    # node count of 1/0.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_path} must be an integer >= 1")
    return value


def _require_str_map(field_path: str, value: object) -> dict[str, str]:
    """Validate a ``{str: str}`` map (e.g. Azure tags) from hand-edited config.

    Azure tag keys and values are both strings; reject anything else loudly so
    a malformed ``additionalTags`` block fails at parse time rather than
    surfacing as an opaque CAPZ reconcile error.
    """
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
    """Parse the ``childConfig.azureWorkload`` map into a typed spec.

    Symmetric with :func:`build_azure_workload_child_config`. The init-stack
    passes ``childConfig`` through as a plain ``dict``; this reader pulls the
    workload-cluster keys back out and applies the module defaults for any
    omitted ``aks`` sizing fields.
    """
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
    """Build the ``{azureWorkload: {...}}`` dict for PKOBootstrap(config=...).

    Symmetric with :func:`parse_azure_workload_spec`. ``additionalTags`` and
    the ``aks`` sizing keys are omitted from the wire shape when empty/``None``
    so the parser falls back to the module defaults — i.e. an operator can
    revert to "use the default" by removing the config key, not by re-stating
    the default value.

    The returned dict has a single top-level key; the caller merges it with
    :func:`stacks.control_plane.control_plane_azure.build_control_plane_azure_child_config`
    (whose key is ``azure``) before handing the union to ``PKOBootstrap``.
    """
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


# ---------------------------------------------------------------------------
# CAPZ managed-cluster CR shapes
# ---------------------------------------------------------------------------

# Core CAPI types (Cluster, MachinePool) on v1beta1 to match the CAPZ v1.23.2
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
# ``Cluster.spec.clusterNetwork.services.cidrBlocks`` — matches the upstream
# AKS template. For a managed control plane this is largely informational
# (AKS manages the real service CIDR), but the CAPI contract wants it set.
_SERVICE_CIDR = "192.168.0.0/16"
# Azure CNI (VNet-integrated pod IPs). The other supported value is
# ``kubenet``; Azure CNI is the chosen default for this project.
_NETWORK_PLUGIN = "azure"
# AKS agent pool name. AKS restricts Linux pool names to <=12 lowercase
# alphanumerics; ``pool0`` complies. The CR's metadata.name may be longer.
_NODE_POOL_NAME = "pool0"
_NODE_POOL_MODE = "System"

_WAIT_FOR_ANNOTATION = "pulumi.com/waitFor"
_WAIT_FOR_READY = "condition=Ready"

_DNS_LABEL_MAX_LENGTH = 63
_DNS_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def _resource_name(instance: str, suffix: str | None = None) -> str:
    """Derive a DNS-label-safe CR name from the tenant instance.

    Mirrors the helper in :mod:`workload_cluster_local_local`. With ``suffix``
    omitted, returns the sanitized instance; with a suffix, returns
    ``<instance>-<suffix>`` truncated to the 63-char DNS-label limit.
    """
    normalized = _DNS_LABEL_INVALID_CHARS.sub("-", instance.lower()).strip("-")
    if not normalized:
        raise ValueError(
            "instance must contain at least one alphanumeric character"
        )
    if suffix is None:
        return normalized[:_DNS_LABEL_MAX_LENGTH].rstrip("-")
    max_instance_length = _DNS_LABEL_MAX_LENGTH - len(suffix) - 1
    normalized = normalized[:max_instance_length].rstrip("-")
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


def _azure_managed_machine_pool_spec(
    *, sku: str, additional_tags: Mapping[str, str]
) -> dict[str, object]:
    """``AzureManagedMachinePool.spec`` — the AKS System-mode agent pool.

    ``additional_tags`` lands on ``spec.additionalTags`` and is stamped by
    CAPZ onto the agent pool's underlying VMSS. This is the per-machine-pool
    tag surface used to satisfy the org Azure Policy that requires an
    ``Owner`` tag on the VMs (AKS does not reliably propagate cluster-level
    tags down to the node VMSS). Omitted from the spec when empty so an
    operator can drop the tag by clearing config.
    """
    spec: dict[str, object] = {
        "mode": _NODE_POOL_MODE,
        "name": _NODE_POOL_NAME,
        "sku": sku,
    }
    if additional_tags:
        spec["additionalTags"] = dict(additional_tags)
    return spec


class WorkloadClusterAzureAKS(pulumi.ComponentResource):
    """Provision a single AKS managed cluster via the CAPZ managed CR set.

    Args:
        name: Pulumi resource name; used as a prefix for the child CRs.
        instance: Tenant instance name; sanitized into the CR/AKS cluster
            name (and ``<name>-pool0`` for the agent pool).
        subscription_id: Azure subscription the AKS cluster is billed to.
            Threaded in from the Phase 1 identity spec by ``InitStackAzure``
            (it is not duplicated in the workload config block).
        identity_name / identity_namespace: name + namespace of the Phase 1
            ``AzureClusterIdentity`` CR, referenced by the AMCP's
            ``identityRef``. These are ``Output`` values from
            ``ControlPlaneAzure``.
        location / resource_group / kubernetes_version / node_sku /
            node_count: from the parsed :class:`AzureWorkloadSpec`.
        additional_tags: extra Azure tags stamped onto the agent pool's VMSS
            via ``AzureManagedMachinePool.spec.additionalTags`` (e.g. the
            ``Owner`` tag required by org policy). From the parsed
            :class:`AzureWorkloadSpec`; empty by default.
        provider: optional management-cluster k8s provider. If None, the
            ambient provider is used (the PKO workspace pod's in-cluster API).

    Outputs:
        cluster_name / control_plane_name / machine_pool_name: echoed CR
            names (resolve at plan time) for downstream references.
        control_plane_ready: ``Output[bool]`` bound to the AMCP's id, which —
            because the AMCP carries ``waitFor=condition=Ready`` — resolves
            only after AKS reports the control plane Ready.
    """

    cluster_name: Output[str]
    control_plane_name: Output[str]
    machine_pool_name: Output[str]
    control_plane_ready: Output[bool]
    todo: Output[str]

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
        node_count: int,
        additional_tags: Mapping[str, str] | None = None,
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:workload:WorkloadClusterAzureAKS", name, props={}, opts=opts
        )

        cluster_name = _resource_name(instance)
        pool_name = _resource_name(instance, _NODE_POOL_NAME)

        def child_opts(
            depends_on: list[pulumi.Resource] | None = None,
        ) -> ResourceOptions:
            return ResourceOptions(
                parent=self, provider=provider, depends_on=depends_on
            )

        # AzureManagedCluster: thin infra marker, empty spec. CAPZ fills in
        # its controlPlaneEndpoint once the AMCP is up.
        azure_managed_cluster = k8s.apiextensions.CustomResource(
            f"{name}-managed-cluster",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind=_AMC_KIND,
            metadata={"name": cluster_name, "namespace": _NAMESPACE},
            spec={},
            opts=child_opts(),
        )

        # AzureManagedMachinePool: the AKS System-mode agent pool. Must exist
        # before the AMCP reconciles so AKS is created with a system pool.
        azure_managed_machine_pool = k8s.apiextensions.CustomResource(
            f"{name}-managed-machine-pool",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind=_AMMP_KIND,
            metadata={"name": pool_name, "namespace": _NAMESPACE},
            spec=_azure_managed_machine_pool_spec(
                sku=node_sku, additional_tags=dict(additional_tags or {})
            ),
            opts=child_opts(),
        )

        # MachinePool: CAPI node-group envelope referencing the AMMP by name.
        machine_pool = k8s.apiextensions.CustomResource(
            f"{name}-machine-pool",
            api_version=_CAPI_API_VERSION,
            kind=_MACHINE_POOL_KIND,
            metadata={"name": pool_name, "namespace": _NAMESPACE},
            spec=_machine_pool_spec(
                cluster_name=cluster_name,
                pool_name=pool_name,
                version=kubernetes_version,
                replicas=node_count,
            ),
            opts=child_opts(depends_on=[azure_managed_machine_pool]),
        )

        # Cluster: the CAPI envelope. Created before the AMCP so that, when
        # the AMCP's blocking create waits on condition=Ready, the owning
        # Cluster already exists for CAPI to wire the ownerRef.
        cluster = k8s.apiextensions.CustomResource(
            f"{name}-cluster",
            api_version=_CAPI_API_VERSION,
            kind=_CLUSTER_KIND,
            metadata={"name": cluster_name, "namespace": _NAMESPACE},
            spec=_cluster_spec(
                control_plane_name=cluster_name,
                infrastructure_name=cluster_name,
            ),
            opts=child_opts(depends_on=[azure_managed_cluster]),
        )

        # AzureManagedControlPlane: the AKS control plane. Created LAST and
        # gated on condition=Ready so ``pulumi up`` blocks through AKS
        # provisioning. depends_on the Cluster (ownerRef prerequisite) and the
        # MachinePool (system pool prerequisite) so neither is missing when
        # CAPZ reconciles the AMCP.
        azure_managed_control_plane = k8s.apiextensions.CustomResource(
            f"{name}-control-plane",
            api_version=_INFRASTRUCTURE_API_VERSION,
            kind=_AMCP_KIND,
            metadata={
                "name": cluster_name,
                "namespace": _NAMESPACE,
                "annotations": {_WAIT_FOR_ANNOTATION: _WAIT_FOR_READY},
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
            opts=child_opts(depends_on=[cluster, machine_pool]),
        )

        self.cluster_name = Output.from_input(cluster_name)
        self.control_plane_name = Output.from_input(cluster_name)
        self.machine_pool_name = Output.from_input(pool_name)
        # ``azure_managed_control_plane.id`` resolves only after the create
        # call returns. Because the CR carries waitFor=condition=Ready, that
        # is after AKS reports the control plane Ready — so this is a genuine
        # readiness signal, not a plan-time echo.
        self.control_plane_ready = azure_managed_control_plane.id.apply(
            lambda _: True
        )
        self.todo = Output.from_input(
            "AKS managed cluster only. Day-2 (workload kubeconfig retrieval, "
            "Slurm/Slinky install) and self-managed clusters (AzureCluster + "
            "KubeadmControlPlane + AzureMachineTemplate) land in later "
            "increments."
        )

        self.register_outputs(
            {
                "cluster_name": self.cluster_name,
                "control_plane_name": self.control_plane_name,
                "machine_pool_name": self.machine_pool_name,
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
