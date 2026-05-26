"""ctlptl-based dynamic-resource bindings for the ``stack_local.py`` target.

Three Pulumi dynamic providers that wrap the ``ctlptl`` and
``cloud-provider-kind`` CLIs:

* :class:`CtlptlRegistry` — Docker-backed image registry.
* :class:`CtlptlCluster` — kind cluster wired to the registry.
* :class:`CloudProviderKind` — host-side daemon that turns ``type:
  LoadBalancer`` Services on kind into host-reachable IPs.

Import surface intentionally narrow: this package is meant to be a
thin compatibility shim around the ctlptl CLI, not a general-purpose
kind-control library.

TODO(multi-target): all three resources are kind-specific by design.
The project-level dispatcher in ``__main__.py`` selects ``stack_local.py``
for the ``local`` stack; future cloud-target stack modules
(``stack_azure.py``, ...) will reach for sibling provider packages under
the same contract: ``cluster_name``, ``context``, ``kubeconfig`` outputs
plus an optional registry handle.
"""

from ctlptl.cloud_provider_kind import CloudProviderKind
from ctlptl.ctlptl_cluster import CtlptlCluster
from ctlptl.ctlptl_registry import CtlptlRegistry

__all__ = ["CloudProviderKind", "CtlptlCluster", "CtlptlRegistry"]
