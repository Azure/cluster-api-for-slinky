"""ctlptl-based dynamic-resource bindings for Kind-backed outer stacks.

Three Pulumi dynamic providers that wrap the ``ctlptl`` and
``cloud-provider-kind`` CLIs:

* :class:`CtlptlRegistry` — Docker-backed image registry.
* :class:`CtlptlCluster` — kind cluster wired to the registry.
* :class:`CloudProviderKind` — host-side daemon that turns ``type:
  LoadBalancer`` Services on kind into host-reachable IPs.

Import surface intentionally narrow: this package is meant to be a
thin compatibility shim around the ctlptl CLI, not a general-purpose
kind-control library.

All three resources are kind-specific by design. The outer stack uses a Kind
management cluster for both local-only and Azure-capable configurations, so it
shares this package through ``stack.py``.
"""

from ctlptl.cloud_provider_kind import CloudProviderKind, CloudProviderKindConfig
from ctlptl.ctlptl_cluster import CtlptlCluster
from ctlptl.ctlptl_registry import CtlptlRegistry
from ctlptl.kind_image_cache import KindImageCache

__all__ = [
  "CloudProviderKind",
  "CloudProviderKindConfig",
  "CtlptlCluster",
  "CtlptlRegistry",
  "KindImageCache",
]
