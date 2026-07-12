"""Kind management-cluster control plane with optional capabilities."""

from __future__ import annotations

from typing import Any

import pulumi
from pydantic import BaseModel, ConfigDict

from lib.config import PulumiConfigModel
from stacks.control_plane.control_plane_config import (
    AzureClusterIdentityConfig,
    ControlPlaneKindConfig,
)
from stacks.control_plane.awx import AWXInstance, AWXOperator, AWXProviderConfig
from stacks.control_plane.awx._configuration import AWXConfiguration
from stacks.control_plane.azure import AzureClusterIdentity
from stacks.control_plane.capi import ClusterAPIOperator
from stacks.control_plane.certmanager import CertManager


class ManagementAWXControlPlaneOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator_namespace: str
    instance_name: str
    service_name: str
    api_url: str
    admin_user: str
    admin_password: str
    admin_password_secret: str
    organization_id: float
    project_id: float
    project_name: str
    scm_credential_id: float
    management_kubernetes_credential_id: float
    dynamic_inventory_id: float
    dynamic_inventory_source_id: float
    cluster_state_job_template_id: float
    ready: bool


class KindAzureControlPlaneSpec(PulumiConfigModel):
    identity: AzureClusterIdentityConfig


class KindAzureControlPlaneOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    cluster_identity_name: str
    cluster_identity_namespace: str
    client_id: str
    tenant_id: str
    ready: bool


class ManagementAWXControlPlane(pulumi.ComponentResource):
    """Build the optional management-cluster AWX control-plane resource graph."""

    outputs: pulumi.Output[ManagementAWXControlPlaneOutputs]

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
        outputs = {
            "operator_namespace": awx_operator.namespace,
            "instance_name": awx_instance.name,
            "service_name": awx_instance.service_name,
            "api_url": awx_provider_config.api_url,
            "admin_user": awx_instance.admin_user,
            "admin_password": awx_provider_config.admin_password,
            "admin_password_secret": awx_instance.admin_password_secret,
            "organization_id": awx_configuration.organization_id,
            "project_id": awx_configuration.project_id,
            "project_name": awx_configuration.project_name,
            "scm_credential_id": awx_configuration.scm_credential_id,
            "management_kubernetes_credential_id": (
                awx_configuration.management_kubernetes_credential_id
            ),
            "dynamic_inventory_id": awx_configuration.dynamic_inventory_id,
            "dynamic_inventory_source_id": awx_configuration.dynamic_inventory_source_id,
            "cluster_state_job_template_id": (
                awx_configuration.cluster_state_job_template_id
            ),
            "ready": ready,
        }
        self.outputs = pulumi.Output.all(**outputs).apply(
            ManagementAWXControlPlaneOutputs.model_validate
        )
        self.register_outputs(outputs)


class KindAzureControlPlane(pulumi.ComponentResource):
    """Azure capability block for a Kind management control plane."""

    outputs: pulumi.Output[KindAzureControlPlaneOutputs]

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
            identity=spec.identity,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[capi],
            ),
        )

        outputs = {
            "cluster_identity_name": azure_cluster_identity.identity_name,
            "cluster_identity_namespace": azure_cluster_identity.identity_namespace,
            "client_id": pulumi.Output.from_input(str(spec.identity.client_id)),
            "tenant_id": pulumi.Output.from_input(str(spec.identity.tenant_id)),
            "ready": azure_cluster_identity.identity_name.apply(lambda _: True),
        }
        self.outputs = pulumi.Output.all(**outputs).apply(
            KindAzureControlPlaneOutputs.model_validate
        )
        self.register_outputs(outputs)


class ControlPlaneKind(pulumi.ComponentResource):
    """Build a Kind management-cluster control plane."""

    cert_manager_namespace: pulumi.Output[str]
    capi_operator_namespace: pulumi.Output[str]
    capi_provider_version: pulumi.Output[str]
    capi_provider_namespaces: dict[str, pulumi.Output[str]]
    infrastructure_providers: pulumi.Output[list[str]]
    awx_enabled: pulumi.Output[bool]
    awx: pulumi.Output[ManagementAWXControlPlaneOutputs] | None
    azure: pulumi.Output[KindAzureControlPlaneOutputs] | None
    control_plane_ready: pulumi.Output[bool]
    todo: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        config: ControlPlaneKindConfig = ControlPlaneKindConfig(),
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:control_plane:ControlPlaneKind",
            name,
            props={},
            opts=opts,
        )
        azure_provider = config.infrastructure_providers.azure
        azure_config = (
            azure_provider
            if azure_provider is not None and azure_provider.enabled
            else None
        )
        azure_spec = (
            KindAzureControlPlaneSpec(
                identity=azure_config.identity,
            )
            if azure_config is not None and azure_config.identity is not None
            else None
        )
        awx_config = config.deployments.awx

        def child_options() -> pulumi.ResourceOptions:
            return pulumi.ResourceOptions(parent=self)

        cert_manager = CertManager("cert-manager", opts=child_options())
        capi = ClusterAPIOperator(
            "cluster-api",
            cert_manager=cert_manager,
            infrastructure_providers=config.infrastructure_providers,
            opts=child_options(),
        )

        awx = (
            ManagementAWXControlPlane(
                "awx",
                flux_source_namespace=awx_config.flux_source_namespace,
                flux_source_name=awx_config.flux_source_name,
                opts=child_options(),
            )
            if awx_config.enabled
            else None
        )
        azure = (
            KindAzureControlPlane(
                "azure",
                spec=azure_spec,
                capi=capi,
                opts=child_options(),
            )
            if azure_spec is not None
            else None
        )

        self.cert_manager_namespace = cert_manager.namespace
        self.capi_operator_namespace = capi.namespace
        self.capi_provider_version = capi.provider_version
        self.capi_provider_namespaces = capi.provider_namespaces
        self.infrastructure_providers = pulumi.Output.from_input(
            list(config.infrastructure_providers.enabled_provider_names())
        )
        self.awx_enabled = pulumi.Output.from_input(awx_config.enabled)
        self.awx = awx.outputs if awx else None
        self.azure = azure.outputs if azure else None

        ready_inputs: list[pulumi.Input[Any]] = [capi.provider_version]
        if self.awx is not None:
            ready_inputs.append(self.awx.apply(lambda outputs: outputs.ready))
        if self.azure is not None:
            ready_inputs.append(self.azure.apply(lambda outputs: outputs.ready))
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
                "awx": (
                    self.awx.apply(lambda outputs: outputs.model_dump())
                    if self.awx is not None
                    else None
                ),
                "azure": (
                    self.azure.apply(lambda outputs: outputs.model_dump())
                    if self.azure is not None
                    else None
                ),
                "control_plane_ready": self.control_plane_ready,
                "todo": self.todo,
            }
        )
