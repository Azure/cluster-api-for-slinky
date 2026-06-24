"""Per-env control-plane component for the ``azure`` env.

Mirrors :mod:`stacks.control_plane.control_plane_local` in structure but
with two deliberate differences:

* **No AWX.** AWX node-level integration is out of scope for the CAPZ
  learning sandbox -- this control plane installs only what's needed
  to understand CAPZ + ASO + the AzureClusterIdentity wiring.
* **azure infrastructure provider.** :class:`capi.ClusterAPIOperator`
  is invoked with ``infrastructure_providers=(\"azure\",)`` instead of
  the default ``(\"docker\",)``. The CAPI Operator's azure
  ``InfrastructureProvider`` CR auto-installs both the CAPZ controller
  (in ``capz-system``) and the Azure Service Operator controller
  alongside it; see https://capz.sigs.k8s.io/topics/aso --
  \"Beginning with CAPZ v1.11.0, ASO's control plane will be installed
  automatically by clusterctl in the capz-system namespace alongside
  CAPZ's control plane components.\"

After CAPZ + ASO converge, this module submits a single
``AzureClusterIdentity`` CR via :class:`azure.AzureClusterIdentity`.
The identity is **UserAssignedMSI**-flavored: it carries no client
secret, and CAPZ obtains tokens at reconcile time from Azure IMDS
(``169.254.169.254``) using the UAMI attached to the host VM the
management ``kind`` cluster runs on.

The three identifiers needed to populate the CR are passed by
:class:`pko._init_stack_azure.InitStackAzure` via the ``spec`` kwarg.
None of these are secrets -- they're just GUIDs that identify the UAMI,
its tenant, and its home subscription.

The Pulumi resource graph relies on the
``pulumi.com/waitFor=condition=Ready`` annotation on the azure
``InfrastructureProvider`` CR (set by :class:`capi.ClusterAPIOperator`)
to gate the AzureClusterIdentity creation behind CRD installation.
Without that wait, the API server would reject the CR with a
\"no matches for kind AzureClusterIdentity\" error.

What Phase 1 does NOT include
-----------------------------
* No workload-cluster resources (``AzureCluster``,
  ``AzureManagedControlPlane``, ``MachinePool``, etc.) -- those belong to
  the workload ``Tenants`` component.
* No Calico or any CNI install (no workload cluster to install it on).
* No autoscaling, no SSH/node customization, no AWX, no Slurm.

Each of those lands in a subsequent phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

import pulumi

from stacks.control_plane.azure import (
    AzureClusterIdentity,
    IMDSPreflightJob,
    IMDSPreflightJobOutputs,
)
from stacks.control_plane.capi import ClusterAPIOperator
from stacks.control_plane.certmanager import CertManager


@dataclass(frozen=True)
class ControlPlaneAzureSpec:
    """UAMI identifiers required to build the Azure control plane.

    All four IDs are non-sensitive GUIDs:

    * ``client_id``        — the UAMI's ``clientId`` (NOT the
      ``principalId``); the CAPZ controller passes this to IMDS to
      select which UAMI to mint a token for.
    * ``principal_id``     — the UAMI's ``principalId`` (a.k.a.
      ``objectId``). Not consumed by CAPZ itself, but required when
      Phase 2 lands ASO ``RoleAssignment``
      / ``FederatedIdentityCredential`` CRs that must reference the
      identity by its Entra object ID. Surface it now (instead of
      requiring a Phase 2 re-config) so adding role assignments later
      is a code change, not a config change.
    * ``tenant_id``        — the Entra tenant the UAMI lives in.
    * ``subscription_id``  — the subscription the UAMI has role
      assignments on. Not read by Phase 1; surfaced here so missing
      values fail at plan time, not deep into Phase 2 when
      ``AzureManagedControlPlane.spec.subscriptionID`` first needs it.

    ``allowed_namespaces`` is an optional restriction on which
    namespaces' workload-cluster CRs may reference the identity:

    * ``None`` (the default) emits ``spec.allowedNamespaces: {}`` on
      the CR — the CAPZ convention for "all namespaces may reference
      this identity". Right default for multi-tenant Phase 2.
    * A non-empty list restricts to those namespaces only.
    * An empty list (``[]``) means "no namespace may reference this
      identity", per the upstream CRD schema; almost never what you
      want, but supported so the contract round-trips cleanly.
    """

    client_id: str
    principal_id: str
    tenant_id: str
    subscription_id: str
    allowed_namespaces: list[str] | None = None
    infrastructure_providers: tuple[str, ...] = ("azure",)
    # When True, skip the in-cluster IMDS preflight Job that
    # ControlPlaneAzure would otherwise schedule into capz-system. Defaults
    # to False so production paths always get the verification.
    skip_in_cluster_preflight: bool = False


# Config keys read out of ``childConfig.azure``. The outer
# stack_azure.py writes these via PKOBootstrap(config=...); the
# InitStackAzure component unpacks them and hands the typed spec to
# ControlPlaneAzure. Single-sourcing the spellings here keeps the two
# endpoints (writer + reader) in lockstep.
CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY = "azure"
_CONFIG_CLIENT_ID = "clientId"
_CONFIG_PRINCIPAL_ID = "principalId"
_CONFIG_TENANT_ID = "tenantId"
_CONFIG_SUBSCRIPTION_ID = "subscriptionId"
_CONFIG_ALLOWED_NAMESPACES = "allowedNamespaces"
_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT = "skipInClusterPreflight"
_CONFIG_INFRASTRUCTURE_PROVIDERS = "infrastructureProviders"

# Azure identifiers (clientId, principalId, tenantId, subscriptionId) are
# all GUIDs in the canonical 8-4-4-4-12 hex layout. Reject anything else
# at plan time so typos fail loudly here instead of surfacing as a
# confusing CAPZ "failed to acquire token" or ASO 4xx ten minutes later.
_GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _require_guid(field_path: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_path} must be a non-empty string")
    if not _GUID_PATTERN.match(value):
        raise ValueError(
            f"{field_path} must be a GUID in 8-4-4-4-12 hex layout; got {value!r}"
        )
    return value


def _parse_allowed_namespaces(
    field_path: str, value: object | None
) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_path} must be a list of namespace names")
    parsed: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                f"{field_path}[{index}] must be a non-empty string"
            )
        parsed.append(entry)
    return parsed


def _parse_infrastructure_providers(
    field_path: str, value: object | None
) -> tuple[str, ...]:
    if value is None:
        return ("azure",)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_path} must be a list of provider names")
    parsed: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                f"{field_path}[{index}] must be a non-empty string"
            )
        parsed.append(entry)
    if "azure" not in parsed:
        raise ValueError(f"{field_path} must include 'azure'")
    return tuple(parsed)


def parse_control_plane_azure_spec(
    value: object | None,
) -> ControlPlaneAzureSpec:
    """Parse the ``childConfig.azure`` map into a typed spec.

    The init-stack passes ``childConfig`` through as a plain ``dict``.
    The outer stack writes it via ``PKOBootstrap(config=...)``, so the
    shape contract has two endpoints: the outer ``stack_azure.py``
    builds the dict using the same key constants this function reads.
    """
    if value is None:
        raise ValueError(
            "missing required Azure control-plane config under "
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r}; the outer "
            "stack_azure.py must pass PKOBootstrap(config={...}) with an "
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r} entry"
        )
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY!r} config must be an "
            f"object; got {type(value).__name__}"
        )

    fields: dict[str, str] = {}
    for config_key, field_name in (
        (_CONFIG_CLIENT_ID, "client_id"),
        (_CONFIG_PRINCIPAL_ID, "principal_id"),
        (_CONFIG_TENANT_ID, "tenant_id"),
        (_CONFIG_SUBSCRIPTION_ID, "subscription_id"),
    ):
        fields[field_name] = _require_guid(
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}.{config_key}",
            value.get(config_key),
        )

    allowed_namespaces = _parse_allowed_namespaces(
        f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}.{_CONFIG_ALLOWED_NAMESPACES}",
        value.get(_CONFIG_ALLOWED_NAMESPACES),
    )
    infrastructure_providers = _parse_infrastructure_providers(
        f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}.{_CONFIG_INFRASTRUCTURE_PROVIDERS}",
        value.get(_CONFIG_INFRASTRUCTURE_PROVIDERS),
    )

    skip_field = value.get(_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT)
    if skip_field is not None and not isinstance(skip_field, bool):
        raise ValueError(
            f"{CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY}."
            f"{_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT} must be a boolean; "
            f"got {type(skip_field).__name__}"
        )

    return ControlPlaneAzureSpec(
        **fields,
        allowed_namespaces=allowed_namespaces,
        infrastructure_providers=infrastructure_providers,
        skip_in_cluster_preflight=bool(skip_field),
    )


def build_control_plane_azure_child_config(
    *,
    client_id: str,
    principal_id: str,
    tenant_id: str,
    subscription_id: str,
    allowed_namespaces: list[str] | None = None,
    infrastructure_providers: tuple[str, ...] = ("azure",),
    skip_in_cluster_preflight: bool = False,
) -> dict[str, object]:
    """Build the dict the outer stack passes via PKOBootstrap(config=...).

    Symmetric with :func:`parse_control_plane_azure_spec` so adding a
    field touches both sides at once. The shape is::

        {CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY: {clientId, principalId,
         tenantId, subscriptionId, allowedNamespaces?,
         skipInClusterPreflight?}}

    ``allowedNamespaces`` is omitted from the dict when ``None`` so the
    init-stack side parses back to ``None`` ("allow all" semantics).
    ``skipInClusterPreflight`` is omitted when False so the default
    safe-path wire shape stays minimal.
    """
    child: dict[str, object] = {
        _CONFIG_CLIENT_ID: client_id,
        _CONFIG_PRINCIPAL_ID: principal_id,
        _CONFIG_TENANT_ID: tenant_id,
        _CONFIG_SUBSCRIPTION_ID: subscription_id,
    }
    if allowed_namespaces is not None:
        child[_CONFIG_ALLOWED_NAMESPACES] = list(allowed_namespaces)
    if infrastructure_providers != ("azure",):
        child[_CONFIG_INFRASTRUCTURE_PROVIDERS] = list(infrastructure_providers)
    if skip_in_cluster_preflight:
        child[_CONFIG_SKIP_IN_CLUSTER_PREFLIGHT] = True
    return {CONTROL_PLANE_AZURE_CHILD_CONFIG_KEY: child}


class ControlPlaneAzure(pulumi.ComponentResource):
    """Build the Azure control-plane resource graph.

    Order and reasoning:

    1. :class:`certmanager.CertManager` -- cert-manager + CRDs.
       Prerequisite for the CAPI Operator's webhooks.
    2. :class:`capi.ClusterAPIOperator` with
       ``infrastructure_providers=(\"azure\",)`` -- installs CAPI core +
       kubeadm bootstrap + kubeadm control-plane + the azure
       infrastructure provider. The CAPI Operator's azure provider
       brings the CAPZ controller and ASO into ``capz-system``, plus
       all CAPZ + ASO CRDs (including ``AzureClusterIdentity``).
    3. :class:`azure.AzureClusterIdentity` -- creates a
       ``UserAssignedMSI``-typed identity that future workload-cluster
       reconciliations will use to obtain Azure AD tokens via the host
       VM's IMDS endpoint. No backing Secret -- the UAMI's credential
       material never leaves Azure.

    cert-manager and the CAPI Operator are independent and install in
    parallel (Pulumi's DAG resolves the actual ordering). The identity
    waits on the CAPI Operator becoming Ready so the
    AzureClusterIdentity CRD exists before submission.
    """

    cert_manager_namespace: pulumi.Output[str]
    capi_operator_namespace: pulumi.Output[str]
    capi_provider_version: pulumi.Output[str]
    capi_provider_namespaces: dict[str, pulumi.Output[str]]
    azure_cluster_identity_name: pulumi.Output[str]
    azure_cluster_identity_namespace: pulumi.Output[str]
    imds_preflight_job: IMDSPreflightJobOutputs | None
    # UAMI identifiers echoed as outputs so Phase 2 components (ASO
    # ``RoleAssignment``, ``AzureManagedControlPlane.spec.subscriptionID``,
    # tenant fan-out) can pull them out of stack state instead of
    # re-reading config. principal_id is the Phase-2-critical addition:
    # CAPZ identifies the UAMI by clientID but ASO role assignments key
    # off the principalID (a.k.a. objectId).
    azure_client_id: pulumi.Output[str]
    azure_principal_id: pulumi.Output[str]
    azure_tenant_id: pulumi.Output[str]
    azure_subscription_id: pulumi.Output[str]
    control_plane_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        spec: ControlPlaneAzureSpec,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ControlPlaneAzure",
            name,
            props={},
            opts=opts,
        )

        def child_options() -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(parent=self)

        cert_manager = CertManager("cert-manager", opts=child_options())
        capi = ClusterAPIOperator(
            "cluster-api",
            cert_manager=cert_manager,
            infrastructure_providers=spec.infrastructure_providers,
            opts=child_options(),
        )

        # UAMI carries no client secret -- the AzureClusterIdentity CR
        # references the UAMI by its clientID, and the CAPZ controller
        # fetches tokens from IMDS at reconcile time.
        #
        # ``spec.allowed_namespaces=None`` (the default) emits the CR
        # with ``spec.allowedNamespaces: {}`` (the CAPZ convention for
        # \"allow all\"). Right default for multi-tenant Phase 2.
        azure_cluster_identity = AzureClusterIdentity(
            "cluster-identity",
            client_id=spec.client_id,
            tenant_id=spec.tenant_id,
            allowed_namespaces=spec.allowed_namespaces,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[capi]),
        )
        # In-cluster IMDS preflight. The Job lands in ``capz-system``
        # (which only exists after the CAPI Operator rolled out CAPZ),
        # so it depends on ``capi`` for namespace existence. The Job
        # carries ``pulumi.com/waitFor=condition=Complete``, meaning
        # pulumi-kubernetes blocks on the Job's exit — a token-mint
        # failure surfaces here as a hard stack error instead of as a
        # silent CAPZ reconcile loop hours later.
        imds_preflight_job = (
            None
            if spec.skip_in_cluster_preflight
            else IMDSPreflightJob(
                "imds-preflight",
                client_id=spec.client_id,
                opts=pulumi.ResourceOptions(parent=self, depends_on=[capi]),
            )
        )

        self.cert_manager_namespace = cert_manager.namespace
        self.capi_operator_namespace = capi.namespace
        self.capi_provider_version = capi.provider_version
        self.capi_provider_namespaces = capi.provider_namespaces
        self.azure_cluster_identity_name = azure_cluster_identity.identity_name
        self.azure_cluster_identity_namespace = (
            azure_cluster_identity.identity_namespace
        )
        self.imds_preflight_job = (
            imds_preflight_job.outputs if imds_preflight_job is not None else None
        )
        # Echo the UAMI identifiers parsed off the spec. Phase 2 consumers
        # (ASO ``RoleAssignment``, ``AzureManagedControlPlane``,
        # workload-cluster fan-out) read them from these outputs so they
        # don't need a second config read.
        self.azure_client_id = pulumi.Output.from_input(spec.client_id)
        self.azure_principal_id = pulumi.Output.from_input(spec.principal_id)
        self.azure_tenant_id = pulumi.Output.from_input(spec.tenant_id)
        self.azure_subscription_id = pulumi.Output.from_input(spec.subscription_id)
        # Gate downstream "control-plane is up" consumers on the things
        # that actually had to exist by Phase 1: the CAPI Operator
        # rolled out (provider_version is its post-release output), the
        # AzureClusterIdentity CR was submitted (its identity_name
        # output is set only after the CRD existed and the CR applied),
        # and the in-cluster IMDS preflight Job completed (its job_name
        # Output is bound to the Job's metadata.name, which
        # pulumi-kubernetes only resolves after the
        # ``pulumi.com/waitFor=condition=Complete`` annotation lifts —
        # i.e. after IMDS actually issued a token from inside
        # ``capz-system``). When ``skip_in_cluster_preflight`` is True,
        # the gate degrades to the pre-Phase-B behavior.
        ready_inputs: list[pulumi.Input[object]] = [
            capi.provider_version,
            azure_cluster_identity.identity_name,
        ]
        if self.imds_preflight_job is not None:
            ready_inputs.append(self.imds_preflight_job.job_name)
        self.control_plane_ready = pulumi.Output.all(*ready_inputs).apply(lambda _: True)
        self.todo = pulumi.Output.from_input(
            "Phase 1 scaffold only -- add workload-cluster Azure "
            "components (AzureManagedControlPlane + MachinePool + "
            "AzureManagedMachinePool) in Phase 2."
        )

        self.register_outputs(
            {
                "cert_manager_namespace": self.cert_manager_namespace,
                "capi_operator_namespace": self.capi_operator_namespace,
                "capi_provider_version": self.capi_provider_version,
                "capi_provider_namespaces": self.capi_provider_namespaces,
                "azure_cluster_identity_name": self.azure_cluster_identity_name,
                "azure_cluster_identity_namespace": (
                    self.azure_cluster_identity_namespace
                ),
                "imds_preflight_job": (
                    self.imds_preflight_job.to_outputs()
                    if self.imds_preflight_job
                    else None
                ),
                "azure_client_id": self.azure_client_id,
                "azure_principal_id": self.azure_principal_id,
                "azure_tenant_id": self.azure_tenant_id,
                "azure_subscription_id": self.azure_subscription_id,
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
