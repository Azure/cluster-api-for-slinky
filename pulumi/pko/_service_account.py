# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Workspace ``ServiceAccount`` + ClusterRoleBindings for inner stacks.

Every PKO Stack CR references a ``spec.serviceAccountName`` that the
workspace pod runs as. The chart-bundled SA (``default/pulumi``) only
exists in the ``default`` namespace and has no useful permissions, so
we create our own:

* ``ServiceAccount/pulumi-runner`` in the PKO namespace.
* ``ClusterRoleBinding`` -> ``system:auth-delegator`` (required by PKO
  docs; used by the operator's TokenReview/SubjectAccessReview machinery
  when reconciling Stack CRs).
* ``ClusterRoleBinding`` -> ``cluster-admin`` (so the inner stacks can
  create namespaces, CRDs, Helm releases, etc.; trim later, see TODO).

Single SA for all inner stack projects (init, control-plane,
workload-cluster). Splitting later — when we have a clear inventory of
what each inner stack actually does — would let us write trimmed ClusterRoles
instead of cluster-admin.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

# The single workspace SA name. Inner Stack CRs reference it by string.
SERVICE_ACCOUNT_NAME = "pulumi-runner"


class WorkspaceServiceAccount(pulumi.ComponentResource):
    """SA + ClusterRoleBindings for inner Pulumi workspace pods.

    Inputs:
        namespace: PKO namespace.
        provider:  Kubernetes provider.

    Outputs:
        service_account_name: Constant ``pulumi-runner``.

    TODO(security): Replace the ``cluster-admin`` binding with three
    purpose-built ClusterRoles (one per inner stack project) once the
    inner stacks' real-world resource set is stable. Tracked here rather
    than on the call site because the trim is a property of THIS
    component's contract.
    """

    service_account_name: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        namespace: pulumi.Input[str],
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            "ca4s:pko:WorkspaceServiceAccount", name, props={}, opts=opts
        )

        sa = k8s.core.v1.ServiceAccount(
            f"{name}-sa",
            metadata={"name": SERVICE_ACCOUNT_NAME, "namespace": namespace},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # Required by PKO docs — used by the operator to delegate token
        # reviews for the workspace pod's SA.
        k8s.rbac.v1.ClusterRoleBinding(
            f"{name}-auth-delegator",
            metadata={"name": f"{SERVICE_ACCOUNT_NAME}-auth-delegator"},
            role_ref={
                "api_group": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "system:auth-delegator",
            },
            subjects=[
                {
                    "kind": "ServiceAccount",
                    "name": SERVICE_ACCOUNT_NAME,
                    "namespace": namespace,
                }
            ],
            opts=ResourceOptions(parent=self, provider=provider, depends_on=[sa]),
        )

        # Broad permission to let inner stacks install operators,
        # CRDs, helm releases, namespaces, etc. The TODO above tracks
        # the trim plan.
        k8s.rbac.v1.ClusterRoleBinding(
            f"{name}-cluster-admin",
            metadata={"name": f"{SERVICE_ACCOUNT_NAME}-cluster-admin"},
            role_ref={
                "api_group": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "cluster-admin",
            },
            subjects=[
                {
                    "kind": "ServiceAccount",
                    "name": SERVICE_ACCOUNT_NAME,
                    "namespace": namespace,
                }
            ],
            opts=ResourceOptions(parent=self, provider=provider, depends_on=[sa]),
        )

        self.service_account_name = sa.metadata.name

        self.register_outputs(
            {"service_account_name": self.service_account_name}
        )
