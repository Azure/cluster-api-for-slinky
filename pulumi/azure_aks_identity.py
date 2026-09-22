"""Outer-stack AKS identities and their resource-scoped permissions."""

from uuid import NAMESPACE_URL, uuid5

import pulumi
import pulumi_azure_native as azure_native

from lib.config import NonEmptyStr, PulumiConfigModel


class AKSIdentityConfig(PulumiConfigModel):
    control_plane_resource_id: NonEmptyStr
    kubelet_resource_id: NonEmptyStr


class AKSClusterIdentities(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        subscription_id: str,
        location: str,
        workload_resource_group: str,
        tags: dict[str, str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:azure:AKSClusterIdentities", name, props={}, opts=opts)
        provider = azure_native.Provider(
            f"{name}-provider", subscription_id=subscription_id,
            opts=pulumi.ResourceOptions(parent=self),
        )
        resource_options = pulumi.ResourceOptions(parent=self, provider=provider)
        group = azure_native.resources.ResourceGroup(
            f"{name}-rg", location=location, tags=tags, opts=resource_options,
        )
        control_plane = azure_native.managedidentity.UserAssignedIdentity(
            f"{name}-control-plane", resource_group_name=group.name,
            location=location, tags=tags, opts=resource_options,
        )
        kubelet = azure_native.managedidentity.UserAssignedIdentity(
            f"{name}-kubelet", resource_group_name=group.name,
            location=location, tags=tags, opts=resource_options,
        )
        grants = []
        for suffix, role_id, scope in (
            ("kubelet-operator", "f1a07417-d97a-45cb-824c-7a7467783830", kubelet.id),
            ("network", "4d97b98b-1d4f-4787-a291-c67834d212e7",
             f"/subscriptions/{subscription_id}/resourceGroups/{workload_resource_group}"),
        ):
            assignment_name = pulumi.Output.all(scope, control_plane.principal_id).apply(
                lambda values, role_id=role_id: str(uuid5(
                    NAMESPACE_URL, f"{values[0].casefold()}:{values[1].casefold()}:{role_id}",
                ))
            )
            grants.append(azure_native.authorization.RoleAssignment(
                f"{name}-{suffix}", scope=scope,
                principal_id=control_plane.principal_id, principal_type="ServicePrincipal",
                role_assignment_name=assignment_name,
                role_definition_id=(
                    f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleDefinitions/{role_id}"
                ),
                opts=resource_options,
            ))
        self.kubelet_principal_id = kubelet.principal_id
        self.config = pulumi.Output.all(
            control_plane.id, kubelet.id, *[grant.id for grant in grants],
        ).apply(lambda values: AKSIdentityConfig(
            control_plane_resource_id=values[0], kubelet_resource_id=values[1],
        ))
        self.register_outputs({
            "config": self.config.apply(lambda config: config.to_config()),
            "kubelet_principal_id": self.kubelet_principal_id,
        })