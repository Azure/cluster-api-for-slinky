# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Stack-owned ephemeral ACR and workload managed-identity pull access."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pulumi
import pulumi_azure_native as azure_native
from azure.mgmt.core.tools import parse_resource_id
from pydantic import field_validator

from lib.config import NonEmptyStr, PulumiConfigModel


_ACR_PULL_ROLE_ID = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
_RBAC_ADMIN_ROLE_ID = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"


class AzureContainerRegistryConfig(PulumiConfigModel):
    """Resolved outer-stack ACR coordinates passed to the PKO init stack."""

    server: NonEmptyStr
    resource_id: NonEmptyStr

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        if "://" in value or "/" in value:
            raise ValueError("server must be a registry authority without a scheme or path")
        return value

class EphemeralAzureContainerRegistry(pulumi.ComponentResource):
    """A disposable Basic ACR owned by the outer development stack."""

    def __init__(
        self,
        name: str,
        *,
        subscription_id: str,
        location: str,
        tags: dict[str, str] | None = None,
        runner_identity_resource_id: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:registry:EphemeralAzureRegistry", name, props={}, opts=opts)
        provider = azure_native.Provider(
            f"{name}-provider",
            subscription_id=subscription_id,
            opts=pulumi.ResourceOptions(parent=self),
        )
        resource_options = pulumi.ResourceOptions(parent=self, provider=provider)
        resource_group = azure_native.resources.ResourceGroup(
            f"{name}-rg", location=location, tags=tags, opts=resource_options,
        )
        registry = azure_native.containerregistry.Registry(
            name,
            resource_group_name=resource_group.name,
            registry_name=resource_group.id.apply(
                lambda resource_id: "ca4s" + uuid5(NAMESPACE_URL, resource_id).hex[:24]
            ),
            location=location,
            sku=azure_native.containerregistry.SkuArgs(name="Basic"),
            admin_user_enabled=False,
            public_network_access="Enabled",
            tags=tags,
            opts=resource_options,
        )
        self.server = registry.login_server
        self.resource_id = registry.id
        delegation = None
        if runner_identity_resource_id is not None:
            identity_parts = parse_resource_id(runner_identity_resource_id)
            identity_provider = azure_native.Provider(
                f"{name}-runner-provider",
                subscription_id=identity_parts["subscription"],
                opts=pulumi.ResourceOptions(parent=self),
            )
            identity = azure_native.managedidentity.get_user_assigned_identity_output(
                resource_group_name=identity_parts["resource_group"],
                resource_name=identity_parts["name"],
                opts=pulumi.InvokeOutputOptions(provider=identity_provider, parent=self),
            )
            delegation = azure_native.authorization.RoleAssignment(
                f"{name}-pull-delegation",
                scope=registry.id,
                principal_id=identity.principal_id,
                principal_type="ServicePrincipal",
                role_assignment_name=registry.id.apply(lambda resource_id: str(uuid5(
                    NAMESPACE_URL,
                    f"{resource_id.casefold()}:{runner_identity_resource_id.casefold()}:acr-pull-delegation",
                ))),
                role_definition_id=(
                    f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
                    f"roleDefinitions/{_RBAC_ADMIN_ROLE_ID}"
                ),
                condition_version="2.0",
                condition=(
                    "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
                    "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                    f"ForAnyOfAnyValues:GuidEquals {{{_ACR_PULL_ROLE_ID}}})) AND "
                    "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
                    "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                    f"ForAnyOfAnyValues:GuidEquals {{{_ACR_PULL_ROLE_ID}}}))"
                ),
                opts=resource_options,
            )
        self.config = pulumi.Output.all(
            server=self.server, resource_id=self.resource_id,
            delegation=delegation.id if delegation is not None else None,
        ).apply(lambda values: AzureContainerRegistryConfig(
            server=values["server"], resource_id=values["resource_id"],
        ))
        self.register_outputs({"server": self.server, "resource_id": self.resource_id})


class AzureContainerRegistryPullAccess(pulumi.ComponentResource):
    """Grant one user-assigned managed identity pull access to an ACR."""

    role_assignment_id: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        registry: AzureContainerRegistryConfig,
        identity_resource_id: pulumi.Input[str] | None = None,
        principal_id: pulumi.Input[str] | None = None,
        azure_client_id: pulumi.Input[str] | None = None,
        azure_tenant_id: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        if (identity_resource_id is None) == (principal_id is None):
            raise ValueError(
                "exactly one of identity_resource_id or principal_id is required"
            )
        if (azure_client_id is None) != (azure_tenant_id is None):
            raise ValueError(
                "azure_client_id and azure_tenant_id must be provided together"
            )
        super().__init__("ca4s:registry:AzurePullAccess", name, props={}, opts=opts)

        registry_parts = parse_resource_id(registry.resource_id)
        provider_credentials = (
            {
                "client_id": azure_client_id,
                "tenant_id": azure_tenant_id,
                "use_msi": True,
            }
            if azure_client_id is not None
            else {}
        )
        registry_provider = azure_native.Provider(
            f"{name}-registry-provider",
            subscription_id=registry_parts["subscription"],
            **provider_credentials,
            opts=pulumi.ResourceOptions(parent=self),
        )
        principal_key: pulumi.Input[str]
        if identity_resource_id is not None:
            identity_parts = pulumi.Output.from_input(identity_resource_id).apply(
                parse_resource_id
            )
            identity_provider = azure_native.Provider(
                f"{name}-identity-provider",
                subscription_id=identity_parts.apply(
                    lambda parts: parts["subscription"]
                ),
                **provider_credentials,
                opts=pulumi.ResourceOptions(parent=self),
            )
            identity = azure_native.managedidentity.get_user_assigned_identity_output(
                resource_group_name=identity_parts.apply(
                    lambda parts: parts["resource_group"]
                ),
                resource_name=identity_parts.apply(lambda parts: parts["name"]),
                opts=pulumi.InvokeOutputOptions(
                    provider=identity_provider,
                    parent=self,
                ),
            )
            principal_id = identity.principal_id
            principal_key = identity_resource_id
        else:
            principal_key = principal_id

        assignment_name = pulumi.Output.from_input(principal_key).apply(
            lambda value: str(
                uuid5(
                    NAMESPACE_URL,
                    f"{registry.resource_id.casefold()}:{value.casefold()}:acr-pull",
                )
            )
        )
        assignment = azure_native.authorization.RoleAssignment(
            f"{name}-acr-pull",
            principal_id=principal_id,
            principal_type="ServicePrincipal",
            role_assignment_name=assignment_name,
            role_definition_id=(
                f"/subscriptions/{registry_parts['subscription']}"
                f"/providers/Microsoft.Authorization/roleDefinitions/{_ACR_PULL_ROLE_ID}"
            ),
            scope=registry.resource_id,
            opts=pulumi.ResourceOptions(parent=self, provider=registry_provider),
        )
        self.role_assignment_id = assignment.id
        self.register_outputs({"role_assignment_id": self.role_assignment_id})
