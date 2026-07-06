"""Kind management-cluster control plane with optional capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pulumi

from lib.outputs import CompositeOutput
from stacks.control_plane.awx import AWXInstance, AWXOperator, AWXProviderConfig
from stacks.control_plane.awx._configuration import AWXConfiguration
from stacks.control_plane.azure import (
    AzureClusterIdentity,
    IMDSPreflightJob,
    IMDSPreflightJobOutputs,
)
from stacks.control_plane.capi import ClusterAPIOperator
from stacks.control_plane.certmanager import CertManager


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


@dataclass(frozen=True)
class KindAzureControlPlaneSpec:
    client_id: str
    principal_id: str
    tenant_id: str
    subscription_id: str
    allowed_namespaces: list[str] | None = None
    skip_in_cluster_preflight: bool = False
    capz_vmss_flex_image: str | None = None


@dataclass(frozen=True)
class KindAzureControlPlaneOutputs(CompositeOutput):
    cluster_identity_name: pulumi.Output[str]
    cluster_identity_namespace: pulumi.Output[str]
    imds_preflight_job: IMDSPreflightJobOutputs | None
    client_id: pulumi.Output[str]
    principal_id: pulumi.Output[str]
    tenant_id: pulumi.Output[str]
    subscription_id: pulumi.Output[str]
    ready: pulumi.Output[bool]


@dataclass(frozen=True)
class ControlPlaneKindSpec:
    infrastructure_providers: tuple[str, ...] = ("docker",)
    enable_awx: bool = True
    azure: KindAzureControlPlaneSpec | None = None


class ManagementAWXControlPlane(pulumi.ComponentResource):
    """Build the optional management-cluster AWX control-plane resource graph."""

    outputs: ManagementAWXControlPlaneOutputs

    def __init__(
        self,
        name: str,
        *,
        flux_source_namespace: pulumi.Input[str],
        flux_source_name: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ManagementAWXControlPlane",
            name,
            props={},
            opts=opts,
        )

        def child_options() -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(parent=self)

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


class KindAzureControlPlane(pulumi.ComponentResource):
    """Azure capability block for a Kind management control plane."""

    outputs: KindAzureControlPlaneOutputs

    def __init__(
        self,
        name: str,
        *,
        spec: KindAzureControlPlaneSpec,
        capi: ClusterAPIOperator,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:KindAzureControlPlane",
            name,
            props={},
            opts=opts,
        )

        azure_cluster_identity = AzureClusterIdentity(
            "cluster-identity",
            client_id=spec.client_id,
            tenant_id=spec.tenant_id,
            allowed_namespaces=spec.allowed_namespaces,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[capi]),
        )
        imds_preflight_job = (
            None
            if spec.skip_in_cluster_preflight
            else IMDSPreflightJob(
                "imds-preflight",
                client_id=spec.client_id,
                opts=pulumi.ResourceOptions(parent=self, depends_on=[capi]),
            )
        )
        imds_preflight_outputs = (
            imds_preflight_job.outputs if imds_preflight_job is not None else None
        )

        ready_inputs: list[pulumi.Input[object]] = [
            azure_cluster_identity.identity_name,
        ]
        if imds_preflight_outputs is not None:
            ready_inputs.append(imds_preflight_outputs.job_name)

        self.outputs = KindAzureControlPlaneOutputs(
            cluster_identity_name=azure_cluster_identity.identity_name,
            cluster_identity_namespace=azure_cluster_identity.identity_namespace,
            imds_preflight_job=imds_preflight_outputs,
            client_id=pulumi.Output.from_input(spec.client_id),
            principal_id=pulumi.Output.from_input(spec.principal_id),
            tenant_id=pulumi.Output.from_input(spec.tenant_id),
            subscription_id=pulumi.Output.from_input(spec.subscription_id),
            ready=pulumi.Output.all(*ready_inputs).apply(lambda _: True),
        )

        self.register_outputs(self.outputs.to_outputs())


class ControlPlaneKind(pulumi.ComponentResource):
    """Build a Kind management-cluster control plane."""

    cert_manager_namespace: pulumi.Output[str]
    capi_operator_namespace: pulumi.Output[str]
    capi_provider_version: pulumi.Output[str]
    capi_provider_namespaces: dict[str, pulumi.Output[str]]
    infrastructure_providers: pulumi.Output[list[str]]
    awx_enabled: pulumi.Output[bool]
    awx: ManagementAWXControlPlaneOutputs | None
    azure: KindAzureControlPlaneOutputs | None
    control_plane_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        flux_source_namespace: pulumi.Input[str] = "",
        flux_source_name: pulumi.Input[str] = "",
        spec: ControlPlaneKindSpec | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ControlPlaneKind",
            name,
            props={},
            opts=opts,
        )
        spec = spec or ControlPlaneKindSpec()

        def child_options() -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(parent=self)

        cert_manager = CertManager("cert-manager", opts=child_options())
        capi = ClusterAPIOperator(
            "cluster-api",
            cert_manager=cert_manager,
            infrastructure_providers=spec.infrastructure_providers,
            azure_vmss_flex_image=(
                spec.azure.capz_vmss_flex_image if spec.azure is not None else None
            ),
            opts=child_options(),
        )

        awx = (
            ManagementAWXControlPlane(
                "awx",
                flux_source_namespace=flux_source_namespace,
                flux_source_name=flux_source_name,
                opts=child_options(),
            )
            if spec.enable_awx
            else None
        )
        azure = (
            KindAzureControlPlane(
                "azure",
                spec=spec.azure,
                capi=capi,
                opts=child_options(),
            )
            if spec.azure is not None
            else None
        )

        self.cert_manager_namespace = cert_manager.namespace
        self.capi_operator_namespace = capi.namespace
        self.capi_provider_version = capi.provider_version
        self.capi_provider_namespaces = capi.provider_namespaces
        self.infrastructure_providers = pulumi.Output.from_input(
            list(spec.infrastructure_providers)
        )
        self.awx_enabled = pulumi.Output.from_input(spec.enable_awx)
        self.awx = awx.outputs if awx else None
        self.azure = azure.outputs if azure else None

        ready_inputs: list[pulumi.Input[Any]] = [capi.provider_version]
        if self.awx is not None:
            ready_inputs.append(self.awx.ready)
        if self.azure is not None:
            ready_inputs.append(self.azure.ready)
        self.control_plane_ready = pulumi.Output.all(*ready_inputs).apply(lambda _: True)
        self.todo = pulumi.Output.from_input(
            "Kind control plane installed cert-manager, CAPI, and requested capabilities."
        )

        self.register_outputs(
            {
                "cert_manager_namespace": self.cert_manager_namespace,
                "capi_operator_namespace": self.capi_operator_namespace,
                "capi_provider_version": self.capi_provider_version,
                "capi_provider_namespaces": self.capi_provider_namespaces,
                "infrastructure_providers": self.infrastructure_providers,
                "awx_enabled": self.awx_enabled,
                "awx": self.awx.to_outputs() if self.awx else None,
                "azure": self.azure.to_outputs() if self.azure else None,
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
