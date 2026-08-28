# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

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
ARM_MANAGEMENT_URL: Final = "https://management.azure.com"
ARM_REQUEST_TIMEOUT_SECONDS: Final = 15.0
ARM_COMPUTE_API_VERSION: Final = "2024-11-01"
ARM_NETWORK_API_VERSION: Final = "2024-05-01"
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
    resource_id: str | None = None


class AzureResourcePlacement(PulumiConfigModel):
    """Azure resource placement facts discovered from IMDS."""

    subscription_id: str
    location: str
    resource_group: str


class AzureHostNetwork(PulumiConfigModel):
    """Authoritative host VNet/subnet identity resolved through Azure ARM."""

    subscription_id: str
    location: str
    vnet_id: str
    vnet_name: str
    vnet_resource_group: str
    subnet_id: str
    subnet_name: str
    subnet_address_prefix: str
    network_security_group_id: str | None = None
    nat_gateway_id: str | None = None
    route_table_id: str | None = None


class AzureEnvironment(PulumiConfigModel):
    """Azure capability discovered from IMDS."""

    credentials: tuple[AzureDiscoveredCredential, ...]
    host_subscription_id: str
    host_location: str
    host_resource_group: str


def _required_str(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _json_or_none(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None


def _imds_instance_payload() -> dict[str, Any] | None:
    try:
        response = requests.get(
            IMDS_INSTANCE_URL,
            headers=IMDS_HEADERS,
            timeout=IMDS_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException:
        return None
    payload = _json_or_none(response)
    return payload if response.status_code == 200 and isinstance(payload, dict) else None


def _arm_resource_payload(
    resource_id: str,
    *,
    api_version: str,
    access_token: str,
) -> dict[str, Any] | None:
    try:
        response = requests.get(
            f"{ARM_MANAGEMENT_URL}{resource_id}",
            params={"api-version": api_version},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=ARM_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException:
        return None
    payload = _json_or_none(response)
    return payload if response.status_code == 200 and isinstance(payload, dict) else None


def _primary_mapping(values: object) -> dict[str, Any] | None:
    if not isinstance(values, list):
        return None
    candidates = [value for value in values if isinstance(value, dict)]
    return next(
        (
            value
            for value in candidates
            if value.get("primary") is True
            or value.get("properties", {}).get("primary") is True
        ),
        candidates[0] if candidates else None,
    )


def _resource_id_part(resource_id: str, collection: str) -> str | None:
    parts = [part for part in resource_id.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == collection.casefold():
            return parts[index + 1]
    return None


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

        resource_id = claims.get("xms_mirid")
        if identity_type == "UserAssignedMSI":
            if not isinstance(resource_id, str) or (
                "/userAssignedIdentities/" not in resource_id
            ):
                return None

        return AzureDiscoveredCredential.model_validate(
            {
                "type": identity_type,
                "client_id": discovered_client_id,
                "tenant_id": tenant_id,
                "resource_id": resource_id,
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
    def discover() -> AzureResourcePlacement | None:
        instance_payload = _imds_instance_payload()
        if instance_payload is None:
            return None

        compute = instance_payload.get("compute")
        if not isinstance(compute, dict):
            return None

        host_subscription_id = _required_str(compute, "subscriptionId")
        host_location = _required_str(compute, "location")
        host_resource_group = _required_str(compute, "resourceGroupName")
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


@overload
def discover_azure_host_network(
    *,
    client_id: UUID | None = None,
    raise_on_missing: Literal[True],
) -> AzureHostNetwork: ...


@overload
def discover_azure_host_network(
    *,
    client_id: UUID | None = None,
    raise_on_missing: Literal[False] = False,
) -> AzureHostNetwork | None: ...


@cache
def discover_azure_host_network(
    *,
    client_id: UUID | None = None,
    raise_on_missing: bool = False,
) -> AzureHostNetwork | None:
    """Resolve this Azure VM's primary VNet/subnet through IMDS and ARM."""

    def discover() -> AzureHostNetwork | None:
        instance = _imds_instance_payload()
        compute = instance.get("compute") if instance is not None else None
        if not isinstance(compute, dict):
            return None

        subscription_id = _required_str(compute, "subscriptionId")
        resource_group = _required_str(compute, "resourceGroupName")
        vm_name = _required_str(compute, "name")
        location = _required_str(compute, "location")
        if not (subscription_id and resource_group and vm_name and location):
            return None

        credential = (
            ManagedIdentityCredential(client_id=str(client_id))
            if client_id is not None
            else ManagedIdentityCredential()
        )
        try:
            access_token = credential.get_token(AZURE_MANAGEMENT_SCOPE).token
        except (AzureError, ValueError):
            return None

        vm_id = (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        )
        vm = _arm_resource_payload(
            vm_id,
            api_version=ARM_COMPUTE_API_VERSION,
            access_token=access_token,
        )
        network_profile = vm.get("properties", {}).get("networkProfile") if vm else None
        primary_nic = (
            _primary_mapping(network_profile.get("networkInterfaces"))
            if isinstance(network_profile, dict)
            else None
        )
        nic_id = _required_str(primary_nic, "id") if primary_nic is not None else None
        if nic_id is None:
            return None

        nic = _arm_resource_payload(
            nic_id,
            api_version=ARM_NETWORK_API_VERSION,
            access_token=access_token,
        )
        nic_properties = nic.get("properties") if nic else None
        primary_ip_configuration = (
            _primary_mapping(nic_properties.get("ipConfigurations"))
            if isinstance(nic_properties, dict)
            else None
        )
        ip_properties = (
            primary_ip_configuration.get("properties")
            if primary_ip_configuration is not None
            else None
        )
        subnet_ref = (
            ip_properties.get("subnet") if isinstance(ip_properties, dict) else None
        )
        subnet_id = _required_str(subnet_ref, "id") if isinstance(subnet_ref, dict) else None
        if subnet_id is None:
            return None

        subnet = _arm_resource_payload(
            subnet_id,
            api_version=ARM_NETWORK_API_VERSION,
            access_token=access_token,
        )
        subnet_properties = subnet.get("properties") if subnet else None
        if not isinstance(subnet_properties, dict):
            return None
        address_prefix = _required_str(subnet_properties, "addressPrefix")
        if address_prefix is None:
            prefixes = subnet_properties.get("addressPrefixes")
            address_prefix = (
                prefixes[0]
                if isinstance(prefixes, list)
                and prefixes
                and isinstance(prefixes[0], str)
                else None
            )

        vnet_name = _resource_id_part(subnet_id, "virtualNetworks")
        subnet_name = _resource_id_part(subnet_id, "subnets")
        vnet_resource_group = _resource_id_part(subnet_id, "resourceGroups")
        if not (address_prefix and vnet_name and subnet_name and vnet_resource_group):
            return None
        vnet_id = subnet_id.rsplit("/subnets/", 1)[0]

        def nested_id(key: str) -> str | None:
            value = subnet_properties.get(key)
            return _required_str(value, "id") if isinstance(value, dict) else None

        return AzureHostNetwork(
            subscription_id=subscription_id,
            location=location,
            vnet_id=vnet_id,
            vnet_name=vnet_name,
            vnet_resource_group=vnet_resource_group,
            subnet_id=subnet_id,
            subnet_name=subnet_name,
            subnet_address_prefix=address_prefix,
            network_security_group_id=nested_id("networkSecurityGroup"),
            nat_gateway_id=nested_id("natGateway"),
            route_table_id=nested_id("routeTable"),
        )

    network = discover()
    if network is None and raise_on_missing:
        raise ValueError("azure host network discovery found no usable host VNet/subnet")
    return network


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