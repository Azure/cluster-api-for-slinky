"""ctlptl-based dynamic-resource bindings for the ca4s-infra-local stack.

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
When the umbrella entrypoint grows a ``cluster_provider`` dispatch (see
the TODO in ``__main__.py``), this package becomes one of several
sibling provider packages (``aws_eks``, ``gke``, ...) under the same
contract: ``cluster_name``, ``context``, ``kubeconfig`` outputs plus an
optional registry handle.
"""

from ctlptl.cloud_provider_kind import CloudProviderKind
from ctlptl.ctlptl_cluster import CtlptlCluster
from ctlptl.ctlptl_registry import CtlptlRegistry

__all__ = ["CloudProviderKind", "CtlptlCluster", "CtlptlRegistry"]
