"""Per-env control-plane component for the ``local`` env.

Runs inside a PKO workspace pod with ``cluster-admin`` on the
management cluster (via ``pulumi-runner`` SA). Its job is to land the
tenant-AGNOSTIC management-cluster operators:

* Cluster API Operator + the core, kubeadm bootstrap, kubeadm control-plane,
  and Docker infrastructure providers.
* AWX. Exposed via a ``Service: LoadBalancer``
  serviced by cloud-provider-kind in local; no ingress controller in
  the picture yet.

Slinky CRDs / slurm-operator / Slurm chart are deliberately NOT in
this list: they belong on each tenant's workload cluster (that's
where ``slurm-operator`` reconciles ``NodeSet``s onto CAPI-managed
worker nodes). ``Tenants`` installs those after CAPI brings the
workload cluster up.

None of the resources here touch tenant state — tenants/workload components
produce per-tenant resources.

State backend
-------------
Runs inside the PKO-owned ``ca4s-init`` stack, so control-plane resources share
that stack's ``file:///state`` backend. A separate control-plane Stack boundary
can be reintroduced later if isolated lifecycle/state becomes useful.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pulumi

from lib.outputs import CompositeOutput
from stacks.control_plane.awx import AWXInstance, AWXOperator, AWXProviderConfig
from stacks.control_plane.awx._configuration import AWXConfiguration
from stacks.control_plane.capi import ClusterAPIOperator
from stacks.control_plane.certmanager import CertManager


CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY = "controlPlane"
_CONFIG_AWX = "awx"
_CONFIG_ENABLED = "enabled"
_LEGACY_LOCAL_AWX_CONTROL_PLANE_TYPE = "ca4s:control_plane:LocalAWXControlPlane"


@dataclass(frozen=True)
class ControlPlaneLocalSpec:
    enable_awx: bool = True


@dataclass(frozen=True)
class ManagementAWXControlPlaneOutputs(CompositeOutput):
    operator_namespace: pulumi.Output[str]
    instance_name: pulumi.Output[str]
    service_name: pulumi.Output[str]
    api_url: pulumi.Output[str]
    admin_user: pulumi.Output[str]
    admin_password: pulumi.Output[str]
    admin_password_secret: pulumi.Output[str]
    organization_id: pulumi.Output[float]
    project_id: pulumi.Output[float]
    project_name: pulumi.Output[str]
    scm_credential_id: pulumi.Output[float]
    management_kubernetes_credential_id: pulumi.Output[float]
    dynamic_inventory_id: pulumi.Output[float]
    dynamic_inventory_source_id: pulumi.Output[float]
    cluster_state_job_template_id: pulumi.Output[float]
    ready: pulumi.Output[bool]


def _require_mapping(field_path: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_path} must be an object")
    return value


def parse_control_plane_local_spec(value: object | None) -> ControlPlaneLocalSpec:
    if value is None:
        return ControlPlaneLocalSpec()
    if isinstance(value, ControlPlaneLocalSpec):
        return value

    spec = _require_mapping(CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY, value)
    awx_value = spec.get(_CONFIG_AWX)
    if awx_value is None:
        return ControlPlaneLocalSpec()

    awx = _require_mapping(
        f"{CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY}.{_CONFIG_AWX}",
        awx_value,
    )
    enabled = awx.get(_CONFIG_ENABLED, True)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"{CONTROL_PLANE_LOCAL_CHILD_CONFIG_KEY}.{_CONFIG_AWX}.{_CONFIG_ENABLED} "
            "must be a boolean"
        )
    return ControlPlaneLocalSpec(enable_awx=enabled)


class ManagementAWXControlPlane(pulumi.ComponentResource):
    """Build the optional management-cluster AWX control-plane resource graph."""

    outputs: ManagementAWXControlPlaneOutputs

    def __init__(
        self,
        name: str,
        *,
        flux_source_namespace: pulumi.Input[str],
        flux_source_name: pulumi.Input[str],
        legacy_parent: pulumi.Resource | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ManagementAWXControlPlane",
            name,
            props={},
            opts=opts,
        )

        def child_options() -> pulumi.ResourceOptions:
            aliases = [pulumi.Alias(parent=legacy_parent)] if legacy_parent else None
            return pulumi.ResourceOptions(parent=self, aliases=aliases)

        awx_operator = AWXOperator("awx-operator", opts=child_options())
        awx_instance = AWXInstance(
            "awx-instance",
            operator=awx_operator,
            opts=child_options(),
        )
        awx_provider_config = AWXProviderConfig(
            "awx-api",
            instance=awx_instance,
            opts=child_options(),
        )
        awx_configuration = AWXConfiguration(
            "awx-configuration",
            provider_config=awx_provider_config,
            flux_source_namespace=flux_source_namespace,
            flux_source_name=flux_source_name,
            opts=child_options(),
        )

        ready = pulumi.Output.all(
            awx_configuration.project_id,
            awx_configuration.management_kubernetes_credential_id,
            awx_configuration.dynamic_inventory_source_id,
            awx_configuration.cluster_state_job_template_id,
        ).apply(lambda _: True)
        self.outputs = ManagementAWXControlPlaneOutputs(
            operator_namespace=awx_operator.namespace,
            instance_name=awx_instance.name,
            service_name=awx_instance.service_name,
            api_url=awx_provider_config.api_url,
            admin_user=awx_instance.admin_user,
            admin_password=awx_provider_config.admin_password,
            admin_password_secret=awx_instance.admin_password_secret,
            organization_id=awx_configuration.organization_id,
            project_id=awx_configuration.project_id,
            project_name=awx_configuration.project_name,
            scm_credential_id=awx_configuration.scm_credential_id,
            management_kubernetes_credential_id=(
                awx_configuration.management_kubernetes_credential_id
            ),
            dynamic_inventory_id=awx_configuration.dynamic_inventory_id,
            dynamic_inventory_source_id=(
                awx_configuration.dynamic_inventory_source_id
            ),
            cluster_state_job_template_id=(
                awx_configuration.cluster_state_job_template_id
            ),
            ready=ready,
        )

        self.register_outputs(self.outputs.to_outputs())


class ControlPlaneLocal(pulumi.ComponentResource):
    """Build the local control-plane resource graph."""

    cert_manager_namespace: pulumi.Output[str]
    capi_operator_namespace: pulumi.Output[str]
    capi_provider_version: pulumi.Output[str]
    capi_provider_namespaces: dict[str, pulumi.Output[str]]
    awx_enabled: pulumi.Output[bool]
    awx: ManagementAWXControlPlaneOutputs | None
    control_plane_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        flux_source_namespace: pulumi.Input[str],
        flux_source_name: pulumi.Input[str],
        enable_awx: bool = True,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ControlPlaneLocal",
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
            opts=child_options(),
        )

        awx = (
            ManagementAWXControlPlane(
                "awx",
                flux_source_namespace=flux_source_namespace,
                flux_source_name=flux_source_name,
                legacy_parent=self,
                opts=pulumi.ResourceOptions(
                    parent=self,
                    aliases=[
                        pulumi.Alias(type_=_LEGACY_LOCAL_AWX_CONTROL_PLANE_TYPE),
                    ],
                ),
            )
            if enable_awx
            else None
        )

        self.cert_manager_namespace = cert_manager.namespace
        self.capi_operator_namespace = capi.namespace
        self.capi_provider_version = capi.provider_version
        self.capi_provider_namespaces = capi.provider_namespaces
        self.awx_enabled = pulumi.Output.from_input(enable_awx)
        self.awx = awx.outputs if awx else None
        ready_inputs: list[pulumi.Input[Any]] = [capi.provider_version]
        if self.awx:
            ready_inputs.append(self.awx.ready)
        self.control_plane_ready = pulumi.Output.all(*ready_inputs).apply(lambda _: True)
        self.todo = pulumi.Output.from_input(
            "Wire AWX tenant inventories, credentials, and Slurm day-2 job templates."
            if enable_awx
            else "AWX disabled; local control plane installs cert-manager and CAPI only."
        )

        self.register_outputs(
            {
                "cert_manager_namespace": self.cert_manager_namespace,
                "capi_operator_namespace": self.capi_operator_namespace,
                "capi_provider_version": self.capi_provider_version,
                "capi_provider_namespaces": self.capi_provider_namespaces,
                "awx_enabled": self.awx_enabled,
                "awx": self.awx.to_outputs() if self.awx else None,
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
