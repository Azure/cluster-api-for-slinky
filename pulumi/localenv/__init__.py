"""Local host environment discovery for local-kind management stacks."""

from ._detect import (
    AzureEnvironment,
    LocalEnvironment,
    ManagementPlaneDefaults,
    discover_local_environment,
)

__all__ = [
    "AzureEnvironment",
    "LocalEnvironment",
    "ManagementPlaneDefaults",
    "discover_local_environment",
]