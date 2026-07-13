"""Unit tests for pure Python Azure environment discovery."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Iterator
from urllib.parse import urlparse
from uuid import UUID

import pytest

import localenv._azure as azure_discovery


_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
_WORKLOAD_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"
_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"
_NIC_ID = (
    f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/host-rg/providers/"
    "Microsoft.Network/networkInterfaces/host-nic"
)
_SUBNET_ID = (
    f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/network-rg/providers/"
    "Microsoft.Network/virtualNetworks/host-vnet/subnets/default"
)
_NSG_ID = (
    f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/network-rg/providers/"
    "Microsoft.Network/networkSecurityGroups/host-nsg"
)


def _clear_azure_discovery_caches() -> None:
    azure_discovery.discover_azure_credentials.cache_clear()
    azure_discovery.discover_azure_resource_placement.cache_clear()
    azure_discovery.discover_azure_host_network.cache_clear()
    azure_discovery.discover_azure_environment.cache_clear()


class _ImdsServer(ThreadingHTTPServer):
    instance_status_code: int = 200
    arm_status_code: int = 200


class _ImdsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/metadata/instance":
            if self.headers.get("Metadata") != "true":
                self._send_json(400, {"error": "missing_metadata_header"})
                return
            self._send_instance()
            return
        if parsed.path.startswith("/subscriptions/"):
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                self._send_json(401, {"error": "missing_authorization"})
                return
            self._send_arm(parsed.path)
            return

        self._send_json(404, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send_instance(self) -> None:
        if self.server.instance_status_code != 200:
            self._send_json(self.server.instance_status_code, {"error": "imds_down"})
            return
        self._send_json(
            200,
            {
                "compute": {
                    "subscriptionId": _SUBSCRIPTION_ID,
                    "location": "westus2",
                    "resourceGroupName": "host-rg",
                    "name": "host-vm",
                },
            },
        )

    def _send_arm(self, path: str) -> None:
        if self.server.arm_status_code != 200:
            self._send_json(self.server.arm_status_code, {"error": "arm_down"})
            return
        if path.endswith("/virtualMachines/host-vm"):
            self._send_json(
                200,
                {
                    "properties": {
                        "networkProfile": {
                            "networkInterfaces": [
                                {"id": f"{_NIC_ID}-secondary"},
                                {"id": _NIC_ID, "properties": {"primary": True}},
                            ]
                        }
                    }
                },
            )
            return
        if path.endswith("/networkInterfaces/host-nic"):
            self._send_json(
                200,
                {
                    "properties": {
                        "ipConfigurations": [
                            {
                                "name": "secondary",
                                "properties": {"primary": False},
                            },
                            {
                                "name": "primary",
                                "properties": {
                                    "primary": True,
                                    "subnet": {"id": _SUBNET_ID},
                                },
                            },
                        ]
                    }
                },
            )
            return
        if path.endswith("/virtualNetworks/host-vnet/subnets/default"):
            self._send_json(
                200,
                {
                    "properties": {
                        "addressPrefix": "10.0.0.0/24",
                        "networkSecurityGroup": {"id": _NSG_ID},
                    }
                },
            )
            return
        self._send_json(404, {"error": "not_found"})

    def _send_json(self, status_code: int, body: object) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@contextmanager
def _mock_imds(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance_status_code: int = 200,
    arm_status_code: int = 200,
) -> Iterator[None]:
    server = _ImdsServer(("127.0.0.1", 0), _ImdsHandler)
    server.instance_status_code = instance_status_code
    server.arm_status_code = arm_status_code
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    _clear_azure_discovery_caches()
    monkeypatch.setattr(
        azure_discovery,
        "IMDS_INSTANCE_URL",
        f"{base_url}/metadata/instance?api-version=2021-02-01",
    )
    monkeypatch.setattr(azure_discovery, "ARM_MANAGEMENT_URL", base_url)
    try:
        yield
    finally:
        _clear_azure_discovery_caches()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class _FakeAccessToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeManagedIdentityCredential:
    client_id: str | None = None
    instances: list[_FakeManagedIdentityCredential] = []
    scopes: list[str] = []

    def __init__(self, *, client_id: str | None = None) -> None:
        self.client_id = client_id
        self.instances.append(self)

    def get_token(self, scope: str) -> _FakeAccessToken:
        self.scopes.append(scope)
        return _FakeAccessToken(
            _jwt({"tid": _TENANT_ID, "appid": self.client_id or _CLIENT_ID})
        )


class _FakeWorkloadIdentityCredential:
    scopes: list[str] = []

    def get_token(self, scope: str) -> _FakeAccessToken:
        self.scopes.append(scope)
        return _FakeAccessToken(_jwt({"tid": _TENANT_ID, "azp": _WORKLOAD_CLIENT_ID}))


def _mock_azure_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_azure_discovery_caches()
    monkeypatch.setattr(
        azure_discovery,
        "ManagedIdentityCredential",
        _FakeManagedIdentityCredential,
    )
    _clear_azure_discovery_caches()
    monkeypatch.setattr(
        azure_discovery,
        "WorkloadIdentityCredential",
        _FakeWorkloadIdentityCredential,
    )


def test_azure_environment_discovery_returns_none_without_imds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_identity(monkeypatch)
    with _mock_imds(monkeypatch, instance_status_code=404):
        env = azure_discovery.discover_azure_environment()

    assert env is None


def test_azure_resource_placement_discovery_can_raise_without_imds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _mock_imds(monkeypatch, instance_status_code=404):
        with pytest.raises(ValueError, match="azure resource placement discovery"):
            azure_discovery.discover_azure_resource_placement(raise_on_missing=True)


def test_azure_environment_discovery_returns_credentials_and_resource_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_identity(monkeypatch)
    _FakeManagedIdentityCredential.instances = []
    _FakeManagedIdentityCredential.scopes = []
    _FakeWorkloadIdentityCredential.scopes = []

    with _mock_imds(monkeypatch):
        env = azure_discovery.discover_azure_environment()

    assert env is not None
    assert env.credentials == (
        azure_discovery.AzureDiscoveredCredential(
            type="UserAssignedMSI",
            client_id=_CLIENT_ID,
            tenant_id=_TENANT_ID,
        ),
        azure_discovery.AzureDiscoveredCredential(
            type="WorkloadIdentity",
            client_id=_WORKLOAD_CLIENT_ID,
            tenant_id=_TENANT_ID,
        ),
    )
    assert env.host_subscription_id == _SUBSCRIPTION_ID
    assert env.host_location == "westus2"
    assert env.host_resource_group == "host-rg"
    assert _FakeManagedIdentityCredential.scopes == [
        azure_discovery.AZURE_MANAGEMENT_SCOPE
    ]
    assert _FakeWorkloadIdentityCredential.scopes == [
        azure_discovery.AZURE_MANAGEMENT_SCOPE
    ]


def test_azure_discovery_passes_client_id_hint_to_managed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_identity(monkeypatch)
    _FakeManagedIdentityCredential.instances = []
    _FakeManagedIdentityCredential.scopes = []
    _FakeWorkloadIdentityCredential.scopes = []
    _clear_azure_discovery_caches()

    with _mock_imds(monkeypatch):
        env = azure_discovery.discover_azure_environment(
            client_id=UUID(_CLIENT_ID)
        )

    assert env is not None
    assert _FakeManagedIdentityCredential.instances[0].client_id == _CLIENT_ID


def test_azure_host_network_discovery_resolves_primary_subnet_through_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_identity(monkeypatch)
    _FakeManagedIdentityCredential.instances = []
    _FakeManagedIdentityCredential.scopes = []

    with _mock_imds(monkeypatch):
        network = azure_discovery.discover_azure_host_network(
            client_id=UUID(_CLIENT_ID),
            raise_on_missing=True,
        )

    assert network == azure_discovery.AzureHostNetwork(
        subscription_id=_SUBSCRIPTION_ID,
        location="westus2",
        vnet_id=_SUBNET_ID.rsplit("/subnets/", 1)[0],
        vnet_name="host-vnet",
        vnet_resource_group="network-rg",
        subnet_id=_SUBNET_ID,
        subnet_name="default",
        subnet_address_prefix="10.0.0.0/24",
        network_security_group_id=_NSG_ID,
    )
    assert _FakeManagedIdentityCredential.instances[0].client_id == _CLIENT_ID
    assert _FakeManagedIdentityCredential.scopes == [
        azure_discovery.AZURE_MANAGEMENT_SCOPE
    ]


def test_azure_host_network_discovery_can_raise_when_arm_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_azure_identity(monkeypatch)

    with _mock_imds(monkeypatch, arm_status_code=404):
        with pytest.raises(ValueError, match="azure host network discovery"):
            azure_discovery.discover_azure_host_network(raise_on_missing=True)


def _jwt(claims: dict[str, object]) -> str:
    header = _base64url({"alg": "none", "typ": "JWT"})
    payload = _base64url(claims)
    return f"{header}.{payload}."


def _base64url(value: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(value).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")