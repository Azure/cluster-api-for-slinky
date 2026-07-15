"""AWX API provider configuration for the management-cluster AWX instance."""

from __future__ import annotations

import pulumi
import pulumi_awx as awx
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from ._instance import AWXInstance

try:
    from awx_api_readiness import (
        AWX_ADMIN_PASSWORD_SECRET_KEY,
        AWXAPIReadiness,
        _AWXAPIReadinessProvider,
        awx_api_url,
        awx_ready_instance_count,
        decode_secret_data_value,
    )
except ModuleNotFoundError:
    from stacks.init.awx_api_readiness import (
        AWX_ADMIN_PASSWORD_SECRET_KEY,
        AWXAPIReadiness,
        _AWXAPIReadinessProvider,
        awx_api_url,
        awx_ready_instance_count,
        decode_secret_data_value,
    )


class AWXProviderConfig(pulumi.ComponentResource):
    """Read AWX admin credentials and expose an AWX API provider."""

    api_url: Output[str]
    admin_user: Output[str]
    admin_password: Output[str]
    admin_password_secret: Output[str]
    provider: awx.Provider

    def __init__(
        self,
        name: str,
        *,
        instance: AWXInstance,
        k8s_provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:control_plane:AWXProviderConfig", name, props={}, opts=opts)

        admin_password_secret = k8s.core.v1.Secret.get(
            f"{name}-admin-password",
            id=Output.concat(instance.namespace, "/", instance.admin_password_secret),
            opts=ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[instance],
            ),
        )
        admin_password = Output.secret(
            admin_password_secret.data.apply(
                lambda data: decode_secret_data_value(
                    data,
                    AWX_ADMIN_PASSWORD_SECRET_KEY,
                )
            )
        )
        api_url = Output.all(instance.namespace, instance.service_name).apply(
            lambda args: awx_api_url(namespace=args[0], service_name=args[1])
        )
        api_readiness = AWXAPIReadiness(
            f"{name}-api-ready",
            api_url=api_url,
            username=instance.admin_user,
            password=admin_password,
            opts=ResourceOptions(parent=self, depends_on=[admin_password_secret]),
        )
        awx_provider = awx.Provider(
            f"{name}-provider",
            hostname=api_url,
            username=instance.admin_user,
            password=admin_password,
            insecure=True,
            opts=ResourceOptions(parent=self, depends_on=[api_readiness]),
        )

        self.api_url = api_url
        self.admin_user = instance.admin_user
        self.admin_password = admin_password
        self.admin_password_secret = instance.admin_password_secret
        self.provider = awx_provider

        self.register_outputs(
            {
                "api_url": self.api_url,
                "admin_user": self.admin_user,
                "admin_password": self.admin_password,
                "admin_password_secret": self.admin_password_secret,
            }
        )