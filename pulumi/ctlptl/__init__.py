"""ctlptl-based dynamic-resource bindings for Kind-backed outer stacks.

Pulumi dynamic providers that wrap the ``ctlptl`` and
``cloud-provider-kind`` CLIs:

* :class:`CtlptlRegistry` — Docker-backed image registry.
* :class:`CtlptlCustomRegistryImage` — source-ref image built into a custom registry.
* :class:`CtlptlCustomRegistryOCIArtifact` — CAPZ provider artifacts built into a custom registry.
* :class:`CtlptlCluster` — kind cluster wired to the registry.
* :class:`CtlptlRegistryService` — in-cluster Service for a registry container.
* :class:`CloudProviderKind` — host-side daemon that turns ``type:
  LoadBalancer`` Services on kind into host-reachable IPs.

Import surface intentionally narrow: this package is meant to be a
thin compatibility shim around the ctlptl CLI, not a general-purpose
kind-control library.

These resources are kind-specific by design. The outer stack uses a Kind
management cluster for both local-only and Azure-capable configurations, so it
shares this package through ``stack.py``.
"""

from ctlptl.cloud_provider_kind import CloudProviderKind, CloudProviderKindConfig
from ctlptl.ctlptl_cluster import CtlptlCluster
from ctlptl.ctlptl_custom_registry_image import CtlptlCustomRegistryImage
from ctlptl.ctlptl_custom_registry_oci_artifact import CtlptlCustomRegistryOCIArtifact
from ctlptl.ctlptl_registry import CtlptlRegistry
from ctlptl.ctlptl_registry_service import CtlptlRegistryService

__all__ = [
  "CloudProviderKind",
  "CloudProviderKindConfig",
  "CtlptlCluster",
  "CtlptlCustomRegistryImage",
  "CtlptlCustomRegistryOCIArtifact",
  "CtlptlRegistry",
  "CtlptlRegistryService",
]
