"""AWX API provider configuration for the management-cluster AWX instance."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import time
from typing import Any

import pulumi
import pulumi_awx as awx
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider, UpdateResult
import requests

from ._instance import AWXInstance


AWX_API_SCHEME = "http"
AWX_ADMIN_PASSWORD_SECRET_KEY = "password"
_AWX_API_READY_TIMEOUT_SECONDS = 300
_AWX_API_READY_POLL_INTERVAL_SECONDS = 5
_AWX_DISPATCH_NODE_TYPES = {"control", "hybrid"}
_AWX_READY_NODE_STATE = "ready"


def awx_api_url(*, namespace: str, service_name: str) -> str:
    return f"{AWX_API_SCHEME}://{service_name}.{namespace}.svc.cluster.local"


def decode_secret_data_value(data: Mapping[str, str] | None, key: str) -> str:
    if data is None:
        raise KeyError(f"Secret data[{key!r}] is missing")
    encoded_value = data.get(key)
    if not encoded_value:
        raise KeyError(f"Secret data[{key!r}] is missing")
    return base64.b64decode(encoded_value).decode("utf-8")


def _positive_capacity(value: object) -> bool:
    if not isinstance(value, int | float | str):
        return False
    try:
        return float(value) > 0
    except ValueError:
        return False


def awx_ready_instance_count(payload: object) -> int:
    if not isinstance(payload, Mapping):
        return 0
    results = payload.get("results")
    if not isinstance(results, list):
        return 0

    ready_count = 0
    for instance in results:
        if not isinstance(instance, Mapping):
            continue
        node_type = str(instance.get("node_type") or "")
        if node_type and node_type not in _AWX_DISPATCH_NODE_TYPES:
            continue
        if not instance.get("enabled"):
            continue
        if instance.get("node_state") != _AWX_READY_NODE_STATE:
            continue
        if not _positive_capacity(instance.get("capacity")):
            continue
        ready_count += 1
    return ready_count


class _AWXAPIReadinessProvider(ResourceProvider):
    def _session(self, props: dict[str, Any]) -> requests.Session:
        session = requests.Session()
        session.auth = (str(props["username"]), str(props["password"]))
        return session

    def _url(self, props: dict[str, Any], path: str) -> str:
        return f"{str(props['api_url']).rstrip('/')}{path}"

    def _wait(self, props: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = int(
            props.get("timeout_seconds") or _AWX_API_READY_TIMEOUT_SECONDS
        )
        deadline = time.monotonic() + timeout_seconds
        session = self._session(props)
        last_error = "AWX API has not been checked yet"

        while True:
            try:
                ping_response = session.get(self._url(props, "/api/v2/ping/"), timeout=30)
                ping_response.raise_for_status()
                instances_response = session.get(
                    self._url(props, "/api/v2/instances/"), timeout=30
                )
                instances_response.raise_for_status()
                ready_count = awx_ready_instance_count(instances_response.json())
                if ready_count > 0:
                    return {
                        "api_url": props["api_url"],
                        "username": props["username"],
                        "timeout_seconds": timeout_seconds,
                        "ready_instance_count": ready_count,
                    }
                last_error = "no enabled ready AWX task/control instances with capacity"
            except requests.RequestException as exc:
                last_error = str(exc)

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for AWX API dispatch readiness: " + last_error
                )
            time.sleep(_AWX_API_READY_POLL_INTERVAL_SECONDS)

    def create(self, props: dict[str, Any]) -> CreateResult:
        outs = self._wait(props)
        return CreateResult(id_=f"{props['api_url']}/api-ready", outs=outs)

    def diff(self, _id: str, old: dict[str, Any], new: dict[str, Any]) -> DiffResult:
        keys = ("api_url", "username", "password", "timeout_seconds")
        return DiffResult(changes=any(old.get(key) != new.get(key) for key in keys))

    def update(self, _id: str, _old: dict[str, Any], new: dict[str, Any]) -> UpdateResult:
        return UpdateResult(outs=self._wait(new))


class AWXAPIReadiness(Resource):
    ready_instance_count: pulumi.Output[int]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        username: pulumi.Input[str],
        password: pulumi.Input[str],
        timeout_seconds: pulumi.Input[int] = _AWX_API_READY_TIMEOUT_SECONDS,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            _AWXAPIReadinessProvider(),
            name,
            {
                "api_url": api_url,
                "username": username,
                "password": password,
                "timeout_seconds": timeout_seconds,
            },
            opts,
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