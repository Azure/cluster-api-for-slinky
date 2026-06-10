"""AWX API provider configuration for the management-cluster AWX instance."""

from __future__ import annotations

import base64
from collections.abc import Mapping

import pulumi
import pulumi_awx as awx
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions

from ._instance import AWXInstance


AWX_API_SCHEME = "http"
AWX_ADMIN_PASSWORD_SECRET_KEY = "password"


def awx_api_url(*, namespace: str, service_name: str) -> str:
    return f"{AWX_API_SCHEME}://{service_name}.{namespace}.svc.cluster.local"


def decode_secret_data_value(data: Mapping[str, str] | None, key: str) -> str:
    if data is None:
        raise KeyError(f"Secret data[{key!r}] is missing")
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


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
        awx_provider = awx.Provider(
            f"{name}-provider",
            hostname=api_url,
            username=instance.admin_user,
            password=admin_password,
            verify_ssl=False,
            opts=ResourceOptions(parent=self, depends_on=[admin_password_secret]),
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