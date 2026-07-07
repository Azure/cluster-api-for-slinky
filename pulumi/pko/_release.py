"""Helm OCI install of the Pulumi Kubernetes Operator (PKO).

Owns:
  * The ``pulumi-kubernetes-operator`` Namespace.
  * One ``helm.v3.Release`` of
    ``oci://ghcr.io/pulumi/helm-charts/pulumi-kubernetes-operator``
    pinned to :data:`PKO_CHART_VERSION`.

The chart's defaults are already minimal (single-replica operator, no metrics
service, no webhook). We add only the RBAC needed for PKO to read Flux Source
objects; Flux owns Git authentication, host-key verification, branch polling,
and source artifact production.

Pin policy mirrors the rest of the stack: explicit chart version bumps,
no implicit "latest". Bump :data:`PKO_CHART_VERSION` and review release
notes before letting ``pulumi up`` reconcile it.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

from pko import PKO_NAMESPACE


# Pinned PKO chart. See https://github.com/pulumi/pulumi-kubernetes-operator
# for the release matrix. v2.x is the current major.
PKO_CHART_OCI = "oci://ghcr.io/pulumi/helm-charts/pulumi-kubernetes-operator"
PKO_CHART_VERSION = "2.7.0"
_BOOTSTRAP_HELM_TIMEOUT_SECONDS = 30 * 60
_BOOTSTRAP_HELM_TIMEOUT = "30m"


class PKORelease(pulumi.ComponentResource):
    """Namespace + Helm OCI release of PKO.

    Children:
      * ``Namespace/pulumi-kubernetes-operator``
      * ``helm.sh/v3:Release`` of the pinned chart.

    Outputs:
      * ``namespace``  — the namespace name (constant, surfaced as Output
        for DAG-edge tracking).
      * ``release_status`` — the Release's ``status`` Output, anchored so
        downstream Stack CRs wait for the operator to be Ready.
    """

    namespace: pulumi.Output[str]
    release_status: pulumi.Output[object]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        namespace_resource: pulumi.Resource,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:PKORelease", name, props={}, opts=opts)

        # OCI chart install. ``helm.v3.Release`` treats ``chart`` that
        # starts with ``oci://`` as an OCI ref; no ``repository_opts``
        # needed.
        #
        # The namespace is owned by :class:`pko.pko_bootstrap.PKOBootstrap`
        # (passed in via ``namespace_resource``) rather than this release.
        release = k8s.helm.v3.Release(
            f"{name}-helm",
            chart=PKO_CHART_OCI,
            version=PKO_CHART_VERSION,
            namespace=PKO_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=_BOOTSTRAP_HELM_TIMEOUT_SECONDS,
            values={
                "rbac": {
                    "extraRules": [
                        {
                            "apiGroups": ["source.toolkit.fluxcd.io"],
                            "resources": ["*"],
                            "verbs": ["get", "list", "watch"],
                        },
                    ],
                },
            },
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[namespace_resource],
                custom_timeouts=pulumi.CustomTimeouts(
                    create=_BOOTSTRAP_HELM_TIMEOUT,
                    update=_BOOTSTRAP_HELM_TIMEOUT,
                    delete=_BOOTSTRAP_HELM_TIMEOUT,
                ),
            ),
        )

        self.namespace = pulumi.Output.from_input(PKO_NAMESPACE)
        self.release_status = release.status

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
            }
        )
