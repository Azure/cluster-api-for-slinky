"""Helm install of the AWX Operator on the management cluster.

Owns:
  * The ``awx`` Namespace.
  * One ``helm.v3.Release`` of the ``awx-operator`` chart from
    ``https://ansible-community.github.io/awx-operator-helm/`` pinned to
    :data:`AWX_OPERATOR_CHART_VERSION`.

The chart installs the operator Deployment plus the ``AWX`` /
``AWXBackup`` / ``AWXRestore`` CRDs (``awx.ansible.com``) it reconciles.
This component stops at the operator — the actual ``AWX`` CR that the
operator turns into a running AWX (Deployment + Service +
``<name>-admin-password`` Secret) is a separate ``AWXInstance``
building block layered on top, so the operator install and the instance
spec evolve independently.

Pin policy mirrors the rest of the stack: explicit chart version bumps,
no implicit "latest". The ``awx-operator`` chart version tracks the
operator image version; bump :data:`AWX_OPERATOR_CHART_VERSION` and
review release notes before letting a reconcile pick it up.

Execution context
------------------
Runs inside a PKO workspace pod with ``cluster-admin`` on the
management cluster (via the ``pulumi-runner`` SA), so it talks to the
cluster through the pod's ambient in-cluster kubeconfig — no explicit
provider required. The optional ``provider`` parameter exists only so
tests (or a future out-of-cluster caller) can inject one.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


# Pinned awx-operator chart. See
# https://github.com/ansible/awx-operator for the release matrix; the
# Helm chart version equals the operator version.
AWX_OPERATOR_CHART_REPO = "https://ansible-community.github.io/awx-operator-helm/"
AWX_OPERATOR_CHART_NAME = "awx-operator"
AWX_OPERATOR_CHART_VERSION = "3.2.1"

# Conventional namespace for the operator and the AWX instance it will
# manage. Kept constant: the forthcoming ``AWXInstance`` and
# ``AWXConfiguration`` components assume AWX lives in ``awx``.
AWX_NAMESPACE = "awx"


class AWXOperator(pulumi.ComponentResource):
    """Namespace + Helm release of the AWX Operator.

    Children:
      * ``Namespace/awx``
      * ``helm.sh/v3:Release`` of the pinned chart.

    Outputs:
      * ``namespace`` — chart namespace name (constant, surfaced as an
        Output for DAG-edge tracking).
      * ``release_status`` — the Release's ``status`` Output, anchored so
        the downstream ``AWX`` CR waits for the operator (and its CRDs)
        to be Ready before it is applied.
    """

    namespace: pulumi.Output[str]
    release_status: pulumi.Output[object]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider | None = None,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:control_plane:AWXOperator", name, props={}, opts=opts)

        ns = k8s.core.v1.Namespace(
            f"{name}-ns",
            metadata={"name": AWX_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # Strict upgrade semantics — same rationale as the PKO/ESO/
        # cert-manager releases.
        release = k8s.helm.v3.Release(
            f"{name}-helm",
            chart=AWX_OPERATOR_CHART_NAME,
            version=AWX_OPERATOR_CHART_VERSION,
            repository_opts={"repo": AWX_OPERATOR_CHART_REPO},
            namespace=AWX_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            # No values overrides: the chart's defaults install the
            # operator + CRDs into AWX_NAMESPACE, which is all this
            # component is responsible for. The AWX instance is specced
            # separately by ``AWXInstance``.
            values={},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[ns],
            ),
        )

        self.namespace = pulumi.Output.from_input(AWX_NAMESPACE)
        self.release_status = release.status

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
            }
        )
