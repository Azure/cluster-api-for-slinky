# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import pytest
import pulumi
from pulumi.runtime.rpc import unwrap_rpc_secret
from pydantic import ValidationError

from azure_container_registry import (
    AzureContainerRegistryConfig,
    AzureContainerRegistryPullAccess,
    EphemeralAzureContainerRegistry,
)


@pytest.mark.parametrize("with_runner", [False, True])
def test_ephemeral_acr_provisions_registry_with_admin_disabled(with_runner) -> None:
    resources: list[pulumi.runtime.MockResourceArgs] = []

    class RegistryMocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            resources.append(args)
            outputs = dict(args.inputs)
            outputs.setdefault("name", args.name)
            if args.typ == "azure-native:containerregistry:Registry":
                outputs["name"] = args.inputs["registryName"]
                outputs["loginServer"] = outputs["name"] + ".azurecr.io"
            return f"/subscriptions/sub/resourceGroups/{args.name}", outputs

        def call(self, args):
            assert args.token == "azure-native:managedidentity:getUserAssignedIdentity"
            assert args.args["resourceGroupName"] == "identities"
            assert args.args["resourceName"] == "runner"
            return {"principalId": "runner-principal"}

    pulumi.runtime.set_mocks(RegistryMocks())

    @pulumi.runtime.test
    def check():
        registry = EphemeralAzureContainerRegistry(
            "workload-registry", subscription_id="sub", location="westus2",
            runner_identity_resource_id=(
                "/subscriptions/identity-sub/resourceGroups/identities/providers/Microsoft.ManagedIdentity/userAssignedIdentities/runner"
                if with_runner else None
            ),
        )

        def verify(config):
            assert config.server.endswith(".azurecr.io")
            assert config.resource_id is not None
            resource = next(item for item in resources if item.typ == "azure-native:containerregistry:Registry")
            assert resource.inputs["sku"] == {"name": "Basic"}
            assert resource.inputs["adminUserEnabled"] is False
            assert resource.inputs["registryName"].isalnum()
            assert any(item.typ == "azure-native:resources:ResourceGroup" for item in resources)
            assignments = [item for item in resources if item.typ == "azure-native:authorization:RoleAssignment"]
            assert len(assignments) == int(with_runner)
            if with_runner:
                assignment = assignments[0].inputs
                assert assignment["scope"] == config.resource_id
                assert assignment["principalId"] == "runner-principal"
                assert assignment["roleDefinitionId"].endswith("/f58310d9-a9f6-439a-9e8d-f62e7b41a168")
                assert assignment["conditionVersion"] == "2.0"
                assert "@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId]" in assignment["condition"]
                assert "@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId]" in assignment["condition"]
                assert assignment["condition"].count("7f951dda-4ed3-4680-a7ca-43fe172d538d") == 2

        return registry.config.apply(verify)

    check()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"server": "https://registry.example"}, "without a scheme or path"),
        ({"server": "registry.example/path"}, "without a scheme or path"),
        ({"server": "ca4s.azurecr.io"}, "resourceId"),
    ],
)
def test_registry_rejects_invalid_transport_and_identity(
    data: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        AzureContainerRegistryConfig.model_validate(data)


def test_resolved_registry_coordinates_round_trip_without_credentials() -> None:
    registry = AzureContainerRegistryConfig(
        server="images.azurecr.io",
        resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ContainerRegistry/registries/images",
    )
    assert AzureContainerRegistryConfig.model_validate(registry.to_config()) == registry
    assert set(registry.to_config()) == {"server", "resourceId"}


@pytest.mark.parametrize("identity_kind", ["aks", "azure-byo"])
def test_workload_pull_access_targets_managed_registry_and_node_identity(identity_kind):
    resources = []
    registry_id = "/subscriptions/registry-sub/resourceGroups/acr/providers/Microsoft.ContainerRegistry/registries/ephemeral"

    class AccessMocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            resources.append(args)
            return args.name, args.inputs

        def call(self, args):
            assert args.token == "azure-native:managedidentity:getUserAssignedIdentity"
            assert args.args["resourceGroupName"] == "nodes"
            assert args.args["resourceName"] == "node-identity"
            return {"principalId": "byo-node-principal"}

    pulumi.runtime.set_mocks(AccessMocks())

    @pulumi.runtime.test
    def check():
        identity = (
            {"principal_id": pulumi.Output.from_input("aks-kubelet-principal")}
            if identity_kind == "aks"
            else {"identity_resource_id": pulumi.Output.from_input(
                "/subscriptions/workload-sub/resourceGroups/nodes/providers/Microsoft.ManagedIdentity/userAssignedIdentities/node-identity"
            )}
        )
        access = AzureContainerRegistryPullAccess(
            "pull", registry=AzureContainerRegistryConfig(server="ephemeral.azurecr.io", resource_id=registry_id),
            azure_client_id="runner-client", azure_tenant_id="tenant", **identity,
        )

        def verify(_):
            role = next(item for item in resources if item.typ == "azure-native:authorization:RoleAssignment")
            assert role.inputs["scope"] == registry_id
            assert role.inputs["principalId"] == ("aks-kubelet-principal" if identity_kind == "aks" else "byo-node-principal")
            assert role.inputs["roleDefinitionId"] == (
                "/subscriptions/registry-sub/providers/Microsoft.Authorization/"
                "roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
            )
            providers = [item for item in resources if item.typ == "pulumi:providers:azure-native"]
            assert {item.inputs["subscriptionId"] for item in providers} == (
                {"registry-sub"} if identity_kind == "aks" else {"registry-sub", "workload-sub"}
            )
            assert all(unwrap_rpc_secret(item.inputs["clientId"]) == "runner-client" for item in providers)

        return access.role_assignment_id.apply(verify)

    check()