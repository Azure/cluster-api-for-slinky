"""Unit tests for pure Python local environment discovery."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Iterator
from urllib.parse import parse_qs, urlparse

import pytest

from localenv import discover_local_environment
import localenv._detect as detect


_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
_PRINCIPAL_ID = "22222222-2222-2222-2222-222222222222"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"
_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"


class _ImdsServer(ThreadingHTTPServer):
    token_for_hint: bool = True
    instance_status_code: int = 200


class _ImdsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.headers.get("Metadata") != "true":
            self._send_json(400, {"error": "missing_metadata_header"})
            return

        parsed = urlparse(self.path)
        if parsed.path == "/metadata/instance":
            self._send_instance()
            return

        if parsed.path == "/metadata/identity/oauth2/token":
            self._send_token(parse_qs(parsed.query))
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
                },
            },
        )

    def _send_token(self, query: dict[str, list[str]]) -> None:
        if query.get("client_id") == ["bad-client"] and not self.server.token_for_hint:
            self._send_json(400, {"error": "invalid_request"})
            return
        self._send_json(
            200,
            {
                "access_token": _jwt(
                    {
                        "oid": _PRINCIPAL_ID,
                        "tid": _TENANT_ID,
                    }
                ),
                "client_id": _CLIENT_ID,
            },
        )

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
    token_for_hint: bool = True,
    instance_status_code: int = 200,
) -> Iterator[None]:
    server = _ImdsServer(("127.0.0.1", 0), _ImdsHandler)
    server.token_for_hint = token_for_hint
    server.instance_status_code = instance_status_code
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(
        detect,
        "IMDS_INSTANCE_URL",
        f"{base_url}/metadata/instance?api-version=2021-02-01",
    )
    monkeypatch.setattr(
        detect,
        "IMDS_TOKEN_URL",
        f"{base_url}/metadata/identity/oauth2/token"
        "?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F",
    )
    try:
        yield
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_off_azure_discovers_docker_only(monkeypatch: pytest.MonkeyPatch) -> None:
    with _mock_imds(monkeypatch, instance_status_code=404):
        env = discover_local_environment()

    assert env.azure is None
    assert env.management_defaults.infrastructure_providers == ("docker",)


def test_azure_imds_adds_azure_provider_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _mock_imds(monkeypatch):
        env = discover_local_environment()

    assert env.azure is not None
    assert env.management_defaults.infrastructure_providers == ("docker", "azure")
    assert env.azure.client_id == _CLIENT_ID
    assert env.azure.principal_id == _PRINCIPAL_ID
    assert env.azure.tenant_id == _TENANT_ID
    assert env.azure.subscription_id == _SUBSCRIPTION_ID
    assert env.azure.location == "westus2"
    assert env.azure.resource_group == "host-rg"


def test_config_hints_override_workload_placement_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _mock_imds(monkeypatch):
        env = discover_local_environment(
            azure_subscription_id_hint="55555555-5555-5555-5555-555555555555",
            azure_location_hint="eastus2",
            azure_resource_group_hint="workload-rg",
        )

    assert env.azure is not None
    assert env.azure.subscription_id == "55555555-5555-5555-5555-555555555555"
    assert env.azure.location == "eastus2"
    assert env.azure.resource_group == "workload-rg"
    assert env.azure.host_subscription_id == _SUBSCRIPTION_ID
    assert env.azure.host_location == "westus2"
    assert env.azure.host_resource_group == "host-rg"


def test_bad_client_id_hint_falls_back_to_default_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _mock_imds(monkeypatch, token_for_hint=False):
        env = discover_local_environment(
            azure_client_id_hint="bad-client",
        )

    assert env.azure is not None
    assert env.azure.client_id == _CLIENT_ID
    assert env.management_defaults.infrastructure_providers == ("docker", "azure")
    assert env.warnings == (
        "Configured azureClientId could not mint an IMDS token; falling "
        "back to the default managed identity selected by IMDS.",
    )


def _jwt(claims: dict[str, object]) -> str:
    header = _base64url({"alg": "none", "typ": "JWT"})
    payload = _base64url(claims)
    return f"{header}.{payload}."


def _base64url(value: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(value).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")