"""Azure-side shared components for the control-plane stack.

Phase 1 contents:

* :class:`AzureClusterIdentity` -- ``UserAssignedMSI``-flavored
  ``AzureClusterIdentity`` CR. No backing Secret -- the CAPZ controller
  fetches Azure AD tokens from the host VM's IMDS endpoint at reconcile
  time using the UAMI's clientID. See :mod:`.azure._cluster_identity`
  for the IMDS reachability prerequisite and the comparison against
  ServicePrincipal and WorkloadIdentity flavors.
* :class:`IMDSPreflightJob` -- one-shot ``batch/v1`` Job in
  ``capz-system`` that probes IMDS for a UAMI-bound token from inside
  the same network namespace CAPZ runs in. Gates
  :class:`ControlPlaneAzure`'s ``control_plane_ready`` output.
Future Phase 2+ contents will likely include thin Pulumi wrappers
around the CAPZ workload-cluster shapes (``AzureManagedControlPlane``
/ ``AzureManagedCluster`` / ``AzureManagedMachinePool``) once
workload-cluster components begin landing. The package lives under
``pulumi/stacks/control_plane/`` because the control-plane components
are where its consumers run (alongside sibling subpackages ``awx/``,
``capi/``, ``certmanager/``).
"""

from __future__ import annotations

from ._cluster_identity import AzureClusterIdentity
from ._imds_preflight_job import IMDSPreflightJob, IMDSPreflightJobOutputs


__all__ = [
    "AzureClusterIdentity",
    "IMDSPreflightJob",
    "IMDSPreflightJobOutputs",
]
