"""Pure Python Azure environment discovery for local stack defaulting."""

from __future__ import annotations

import base64
from functools import cache
import json
from typing import Any, Final, Literal, overload, TypeAlias
from uuid import UUID

import requests
from azure.core.exceptions import AzureError
from azure.identity import (
    ManagedIdentityCredential,
    WorkloadIdentityCredential,
)

from lib.config import PulumiConfigModel


IMDS_INSTANCE_URL: Final = (
    "http://169.254.169.254/metadata/instance?api-version=2021-02-01"
)
IMDS_HEADERS: Final = {"Metadata": "true"}
IMDS_TIMEOUT_SECONDS: Final = 5.0
AZURE_MANAGEMENT_SCOPE: Final = "https://management.azure.com/.default"
AzureIdentityType: TypeAlias = Literal["UserAssignedMSI", "WorkloadIdentity"]
AZURE_AMBIENT_IDENTITY_TYPES: Final[tuple[AzureIdentityType, ...]] = (
    "UserAssignedMSI",
    "WorkloadIdentity",
)


class AzureDiscoveredCredential(PulumiConfigModel):
    """Usable ambient Azure identity discovered from the local environment."""

    type: AzureIdentityType
    client_id: str
    tenant_id: str


class AzureResourcePlacement(PulumiConfigModel):
    """Azure resource placement facts discovered from IMDS."""

    subscription_id: str
    location: str
    resource_group: str


class AzureEnvironment(PulumiConfigModel):
    """Azure capability discovered from IMDS."""

    credentials: tuple[AzureDiscoveredCredential, ...]
    host_subscription_id: str
    host_location: str
    host_resource_group: str


@cache
def discover_azure_credentials(
    *,
    identity_types: tuple[AzureIdentityType, ...] = AZURE_AMBIENT_IDENTITY_TYPES,
    client_id: UUID | None = None,
    raise_on_missing: bool = False,
) -> tuple[AzureDiscoveredCredential, ...]:
    def decode_jwt_claims(token: str) -> dict[str, Any]:
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

    def credential_claims(credential: Any) -> dict[str, Any] | None:
        try:
            token = credential.get_token(AZURE_MANAGEMENT_SCOPE)
        except (AzureError, ValueError):
            return None
        return decode_jwt_claims(token.token)

    def client_id_from_claims(claims: dict[str, Any]) -> str | None:
        for claim in ("appid", "azp"):
            value = claims.get(claim)
            if isinstance(value, str) and value:
                return value
        return None

    def discovered_credential(
        identity_type: AzureIdentityType,
        credential: Any,
    ) -> AzureDiscoveredCredential | None:
        claims = credential_claims(credential)
        if claims is None:
            return None

        tenant_id = claims.get("tid")
        discovered_client_id = client_id_from_claims(claims)
        if not isinstance(tenant_id, str) or discovered_client_id is None:
            return None

        return AzureDiscoveredCredential.model_validate(
            {
                "type": identity_type,
                "client_id": discovered_client_id,
                "tenant_id": tenant_id,
            }
        )

    credentials: list[AzureDiscoveredCredential] = []

    if "UserAssignedMSI" in identity_types:
        managed_identity_credential = (
            ManagedIdentityCredential(client_id=str(client_id))
            if client_id is not None
            else ManagedIdentityCredential()
        )
        managed_identity = discovered_credential(
            "UserAssignedMSI",
            managed_identity_credential,
        )
        if managed_identity is not None and (
            client_id is None or managed_identity.client_id == str(client_id)
        ):
            credentials.append(managed_identity)

    if "WorkloadIdentity" in identity_types:
        try:
            workload_identity_credential = WorkloadIdentityCredential()
        except ValueError:
            workload_identity_credential = None
        if workload_identity_credential is not None:
            workload_identity = discovered_credential(
                "WorkloadIdentity",
                workload_identity_credential,
            )
            if workload_identity is not None and (
                client_id is None or workload_identity.client_id == str(client_id)
            ):
                credentials.append(workload_identity)

    if not credentials and raise_on_missing:
        raise ValueError(
            "azure identity discovery found no usable credential matching "
            "configured identity"
        )
    return tuple(credentials)


@overload
def discover_azure_resource_placement(
    *,
    raise_on_missing: Literal[True],
) -> AzureResourcePlacement: ...


@overload
def discover_azure_resource_placement(
    *,
    raise_on_missing: Literal[False] = False,
) -> AzureResourcePlacement | None: ...


@cache
def discover_azure_resource_placement(
    *,
    raise_on_missing: bool = False,
) -> AzureResourcePlacement | None:
    def json_or_none(response: requests.Response) -> Any | None:
        try:
            return response.json()
        except ValueError:
            return None

    def required_str(mapping: dict[str, Any], key: str) -> str | None:
        value = mapping.get(key)
        return value if isinstance(value, str) and value else None

    def discover() -> AzureResourcePlacement | None:
        try:
            instance_response = requests.get(
                IMDS_INSTANCE_URL,
                headers=IMDS_HEADERS,
                timeout=IMDS_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException:
            return None

        instance_payload = json_or_none(instance_response)
        if instance_response.status_code != 200 or not isinstance(
            instance_payload, dict
        ):
            return None

        compute = instance_payload.get("compute")
        if not isinstance(compute, dict):
            return None

        host_subscription_id = required_str(compute, "subscriptionId")
        host_location = required_str(compute, "location")
        host_resource_group = required_str(compute, "resourceGroupName")
        if not (host_subscription_id and host_location and host_resource_group):
            return None

        return AzureResourcePlacement.model_validate(
            {
                "subscription_id": host_subscription_id,
                "location": host_location,
                "resource_group": host_resource_group,
            }
        )

    placement = discover()
    if placement is None and raise_on_missing:
        raise ValueError("azure resource placement discovery found no usable host environment")
    return placement


@cache
def discover_azure_environment(
    *,
    client_id: UUID | None = None,
    raise_on_missing: bool = False,
) -> AzureEnvironment | None:
    def discover() -> AzureEnvironment | None:
        try:
            credentials = discover_azure_credentials(client_id=client_id)
        except ValueError:
            credentials = ()
        if not credentials:
            return None

        placement = discover_azure_resource_placement()
        if placement is None:
            return None

        return AzureEnvironment.model_validate(
            {
                "credentials": tuple(credentials),
                "host_subscription_id": placement.subscription_id,
                "host_location": placement.location,
                "host_resource_group": placement.resource_group,
            }
        )

    azure_environment = discover()
    if azure_environment is None and raise_on_missing:
        raise ValueError(
            "partial azure infrastructure provider config requires "
            "discovered Azure environment"
        )
    return azure_environment