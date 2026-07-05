"""Pure Python discovery for the host backing a local-kind management plane."""

from __future__ import annotations

import base64
import getpass
import json
from typing import Any, Final

import requests

from lib.config import PulumiConfigModel


IMDS_INSTANCE_URL: Final = (
    "http://169.254.169.254/metadata/instance?api-version=2021-02-01"
)
IMDS_TOKEN_URL: Final = (
    "http://169.254.169.254/metadata/identity/oauth2/token"
    "?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F"
)
IMDS_HEADERS: Final = {"Metadata": "true"}
IMDS_TIMEOUT_SECONDS: Final = 5.0


class AzureEnvironment(PulumiConfigModel):
    """Azure capability discovered from IMDS."""

    client_ids: tuple[str, ...]
    tenant_id: str
    host_subscription_id: str
    host_location: str
    host_resource_group: str


class ManagementPlaneDefaults(PulumiConfigModel):
    """Resource-graph defaults derived from local host capabilities."""

    infrastructure_providers: tuple[str, ...]


class LocalEnvironment(PulumiConfigModel):
    """Local-kind host discovery result."""

    local_username: str | None
    azure: AzureEnvironment | None
    management_defaults: ManagementPlaneDefaults
    warnings: tuple[str, ...] = ()


def discover_local_environment() -> LocalEnvironment:
    """Discover host capabilities and derive management-plane defaults.

    The result is intentionally made of plain Python values so stack modules can
    use it while declaring Pulumi resources. Discovery is best-effort for Azure:
    off-Azure hosts simply get the docker provider default, while an Azure host
    with a usable managed identity gets CAPZ added to the provider list.
    """
    warnings: list[str] = []
    azure = _discover_azure_environment()
    return LocalEnvironment(
        local_username=_discover_local_username(),
        azure=azure,
        management_defaults=ManagementPlaneDefaults(
            infrastructure_providers=(
                ("docker", "azure") if azure is not None else ("docker",)
            ),
        ),
        warnings=tuple(warnings),
    )


def _discover_local_username() -> str | None:
    try:
        username = getpass.getuser()
    except OSError:
        return None
    return username or None


def _discover_azure_environment() -> AzureEnvironment | None:
    try:
        instance_response = requests.get(
            IMDS_INSTANCE_URL,
            headers=IMDS_HEADERS,
            timeout=IMDS_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException:
        return None

    instance_payload = _json_or_none(instance_response)
    if instance_response.status_code != 200 or not isinstance(
        instance_payload, dict
    ):
        return None

    compute = instance_payload.get("compute")
    if not isinstance(compute, dict):
        return None

    host_subscription_id = _required_str(compute, "subscriptionId")
    host_location = _required_str(compute, "location")
    host_resource_group = _required_str(compute, "resourceGroupName")
    if not (host_subscription_id and host_location and host_resource_group):
        return None

    token_payload = _request_token()
    if token_payload is None:
        return None

    access_token = token_payload.get("access_token")
    client_id = token_payload.get("client_id")
    if not isinstance(access_token, str) or not isinstance(client_id, str):
        return None

    claims = _decode_jwt_claims(access_token)
    tenant_id = claims.get("tid")
    if not isinstance(tenant_id, str):
        return None

    return AzureEnvironment(
        client_ids=(client_id,),
        tenant_id=tenant_id,
        host_subscription_id=host_subscription_id,
        host_location=host_location,
        host_resource_group=host_resource_group,
    )


def _request_token() -> dict[str, Any] | None:
    try:
        response = requests.get(
            IMDS_TOKEN_URL,
            headers=IMDS_HEADERS,
            timeout=IMDS_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException:
        return None

    payload = _json_or_none(response)
    if response.status_code != 200 or not isinstance(payload, dict):
        return None
    if "error" in payload:
        return None
    return payload


def _json_or_none(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None


def _required_str(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}