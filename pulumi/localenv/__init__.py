# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Local environment discovery helpers."""

from ._azure import (
    AZURE_AMBIENT_IDENTITY_TYPES,
    AZURE_MANAGEMENT_SCOPE,
    AzureDiscoveredCredential,
    AzureEnvironment,
    AzureHostNetwork,
    AzureIdentityType,
    AzureResourcePlacement,
    discover_azure_credentials,
    discover_azure_environment,
    discover_azure_host_network,
    discover_azure_resource_placement,
)
from ._host import discover_local_username

__all__ = [
    "AZURE_AMBIENT_IDENTITY_TYPES",
    "AZURE_MANAGEMENT_SCOPE",
    "AzureDiscoveredCredential",
    "AzureEnvironment",
    "AzureHostNetwork",
    "AzureIdentityType",
    "AzureResourcePlacement",
    "discover_azure_credentials",
    "discover_azure_environment",
    "discover_azure_host_network",
    "discover_azure_resource_placement",
    "discover_local_username",
]