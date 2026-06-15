"""Azure-side shared components for the control-plane stack.

Phase 1 contents:

* :class:`AzureClusterIdentity` \u2014 ``UserAssignedMSI``-flavored
  ``AzureClusterIdentity`` CR. No backing Secret \u2014 the CAPZ controller
  fetches Azure AD tokens from the host VM's IMDS endpoint at reconcile
  time using the UAMI's clientID. See :mod:`.azure._cluster_identity`
  for the IMDS reachability prerequisite and the comparison against
  ServicePrincipal and WorkloadIdentity flavors.
* :class:`IMDSPreflightJob` \u2014 one-shot ``batch/v1`` Job in
  ``capz-system`` that probes IMDS for a UAMI-bound token from inside
  the same network namespace CAPZ runs in. Gates
  :class:`ControlPlaneAzure`'s ``control_plane_ready`` output.
* :func:`check_uami_attached` \u2014 host-side IMDS preflight helper.
  Complement to :class:`IMDSPreflightJob`: runs in the language host
  before any CRs are submitted, so it catches off-Azure / wrong-clientId
  setups at plan time rather than at reconcile time.

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
from ._imds_preflight import ImdsPreflightError, check_uami_attached
from ._imds_preflight_job import IMDSPreflightJob


__all__ = [
    "AzureClusterIdentity",
    "IMDSPreflightJob",
    "ImdsPreflightError",
    "check_uami_attached",
]
