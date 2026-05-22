"""Project-local Pulumi resource types for the ca4s infra-local stack."""

from ca4s_local.cloud_provider_kind import CloudProviderKind
from ca4s_local.ctlptl_cluster import CtlptlCluster
from ca4s_local.ctlptl_registry import CtlptlRegistry

__all__ = ["CloudProviderKind", "CtlptlCluster", "CtlptlRegistry"]
