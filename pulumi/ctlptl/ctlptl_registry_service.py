"""Expose a ctlptl registry container through an in-cluster Service.

The kind node containerd registry wiring lets kubelet pull images from a
Docker-side registry, but CAPI Operator's OCI artifact fetch runs inside the
operator pod. This component gives pods a normal Kubernetes DNS target for the
same registry by discovering the registry container's Docker ``kind`` network
IP and creating a Service/Endpoints pair that points at it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from typing import Optional

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

_RESOURCE_TYPE = "ca4s:local:CtlptlRegistryNetworkAddress"
_DEFAULT_NETWORK_NAME = "kind"
_DEFAULT_REGISTRY_PORT = 5000


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"required binary '{name}' not found in PATH; install it before running pulumi"
        )
    return path


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command {cmd!r} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _container_network_ip(container_name: str, network_name: str) -> str:
    _require_binary("docker")
    result = _run(["docker", "inspect", container_name])
    try:
        inspected = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"docker inspect {container_name!r} returned invalid JSON"
        ) from error
    if not inspected:
        raise RuntimeError(f"docker inspect {container_name!r} returned no containers")
    networks = inspected[0].get("NetworkSettings", {}).get("Networks", {})
    network = networks.get(network_name)
    if network is None:
        available = sorted(str(name) for name in networks.keys())
        raise RuntimeError(
            f"registry container {container_name!r} is not attached to Docker "
            f"network {network_name!r}; attached networks: {available!r}"
        )
    ip_address = str(network.get("IPAddress") or "")
    if not ip_address:
        raise RuntimeError(
            f"registry container {container_name!r} has no IP address on "
            f"Docker network {network_name!r}"
        )
    return ip_address


class _CtlptlRegistryNetworkAddressProvider(ResourceProvider):
    def create(self, props: dict) -> CreateResult:
        container_name = str(props["container_name"])
        network_name = str(props.get("network_name") or _DEFAULT_NETWORK_NAME)
        port = int(props.get("port") or _DEFAULT_REGISTRY_PORT)
        ip_address = _container_network_ip(container_name, network_name)
        return CreateResult(
            id_=f"{container_name}:{network_name}",
            outs={
                "container_name": container_name,
                "network_name": network_name,
                "port": port,
                "ip_address": ip_address,
            },
        )

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        container_name = str(news["container_name"])
        network_name = str(news.get("network_name") or _DEFAULT_NETWORK_NAME)
        port = int(news.get("port") or _DEFAULT_REGISTRY_PORT)
        ip_address = _container_network_ip(container_name, network_name)
        return UpdateResult(
            outs={
                "container_name": container_name,
                "network_name": network_name,
                "port": port,
                "ip_address": ip_address,
            }
        )

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        replaces: list[str] = []
        if news.get("container_name") != olds.get("container_name"):
            replaces.append("container_name")
        if (news.get("network_name") or _DEFAULT_NETWORK_NAME) != (
            olds.get("network_name") or _DEFAULT_NETWORK_NAME
        ):
            replaces.append("network_name")
        old_port = int(olds.get("port") or _DEFAULT_REGISTRY_PORT)
        new_port = int(news.get("port") or _DEFAULT_REGISTRY_PORT)
        return DiffResult(
            changes=bool(replaces) or old_port != new_port,
            replaces=replaces,
            delete_before_replace=True,
        )

    def read(self, id_: str, props: dict) -> ReadResult:
        container_name = str(props["container_name"])
        network_name = str(props.get("network_name") or _DEFAULT_NETWORK_NAME)
        port = int(props.get("port") or _DEFAULT_REGISTRY_PORT)
        try:
            ip_address = _container_network_ip(container_name, network_name)
        except Exception:
            return ReadResult(id_=None, outs={})
        outs = dict(props)
        outs.update(
            {
                "container_name": container_name,
                "network_name": network_name,
                "port": port,
                "ip_address": ip_address,
            }
        )
        return ReadResult(id_=id_, outs=outs)


class CtlptlRegistryNetworkAddress(Resource):
    """Docker network address for a registry container."""

    container_name: Output[str]
    network_name: Output[str]
    port: Output[int]
    ip_address: Output[str]

    def __init__(
        self,
        name: str,
        *,
        container_name: Input[str],
        network_name: Optional[Input[str]] = None,
        port: Optional[Input[int]] = None,
        opts: Optional[ResourceOptions] = None,
    ) -> None:
        super().__init__(
            _CtlptlRegistryNetworkAddressProvider(),
            name,
            {
                "container_name": container_name,
                "network_name": network_name,
                "port": port,
                "ip_address": None,
            },
            opts,
        )


class CtlptlRegistryService(pulumi.ComponentResource):
    """Expose a ctlptl registry container to pods via Service DNS."""

    service_name: Output[str]
    namespace: Output[str]
    url: Output[str]
    ip_address: Output[str]

    def __init__(
        self,
        name: str,
        *,
        registry_name: Input[str],
        namespace: Input[str],
        service_name: Optional[Input[str]] = None,
        network_name: Optional[Input[str]] = None,
        port: Optional[Input[int]] = None,
        provider: Optional[k8s.Provider] = None,
        dependencies: Optional[Sequence[pulumi.Resource]] = None,
        opts: Optional[ResourceOptions] = None,
    ) -> None:
        super().__init__(
            "ca4s:local:CtlptlRegistryService",
            name,
            props={},
            opts=opts,
        )
        registry_port = _DEFAULT_REGISTRY_PORT if port is None else port
        resolved_service_name = registry_name if service_name is None else service_name
        dependency_list = list(dependencies or [])

        address = CtlptlRegistryNetworkAddress(
            f"{name}-address",
            container_name=registry_name,
            network_name=network_name,
            port=registry_port,
            opts=ResourceOptions(parent=self, depends_on=dependency_list),
        )
        service = k8s.core.v1.Service(
            f"{name}-service",
            metadata={"name": resolved_service_name, "namespace": namespace},
            spec={
                "ports": [
                    {
                        "name": "registry",
                        "port": registry_port,
                        "protocol": "TCP",
                        "targetPort": registry_port,
                    }
                ]
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=dependency_list,
            ),
        )
        endpoints = k8s.core.v1.Endpoints(
            f"{name}-endpoints",
            metadata={"name": resolved_service_name, "namespace": namespace},
            subsets=[
                {
                    "addresses": [{"ip": address.ip_address}],
                    "ports": [
                        {
                            "name": "registry",
                            "port": registry_port,
                            "protocol": "TCP",
                        }
                    ],
                }
            ],
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[service, address, *dependency_list],
            ),
        )

        self.service_name = Output.from_input(resolved_service_name)
        self.namespace = Output.from_input(namespace)
        self.ip_address = address.ip_address
        self.url = Output.concat(
            "http://",
            self.service_name,
            ".",
            self.namespace,
            ".svc.cluster.local:",
            registry_port,
        )
        self.register_outputs(
            {
                "service_name": self.service_name,
                "namespace": self.namespace,
                "ip_address": self.ip_address,
                "url": self.url,
                "service": service.metadata["name"],
                "endpoints": endpoints.metadata["name"],
            }
        )