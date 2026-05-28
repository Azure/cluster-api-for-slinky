"""Helm install of cert-manager on the management cluster.

Owns:
  * The ``cert-manager`` Namespace.
  * One ``helm.v3.Release`` of the ``cert-manager`` chart from
    ``https://charts.jetstack.io`` pinned to
    :data:`CERT_MANAGER_CHART_VERSION`, with CRDs enabled.

Why it lives in the control plane
---------------------------------
cert-manager is a prerequisite for admission/validating webhooks
across the stack. Concretely, the ``cluster-api-operator`` chart and
the CAPI provider deployments it reconciles expect cert-manager to be
present for their webhook certificates; AWX can also lean on it for
ingress TLS. Installing it first, as a sibling of the AWX operator,
keeps the dependency explicit and tenant-agnostic.

CRDs
----
We enable the chart-managed CRDs (``crds.enabled=true``) rather than
applying them out of band, so the CRDs share the release lifecycle and
are cleaned up on teardown. cert-manager v1.15+ uses the ``crds.enabled``
value (older charts used ``installCRDs``); keep this in sync if the pin
ever moves below v1.15.

Pin policy mirrors the rest of the stack: explicit chart version bumps,
no implicit "latest". Bump :data:`CERT_MANAGER_CHART_VERSION` and review
release notes before letting a reconcile pick it up.

Execution context
------------------
This module runs inside a PKO workspace pod with ``cluster-admin`` on
the management cluster (via the ``pulumi-runner`` SA). It therefore
talks to the cluster through the pod's ambient in-cluster kubeconfig —
no explicit provider is required. The optional ``provider`` parameter
exists only so tests (or a future out-of-cluster caller) can inject one.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


# Pinned cert-manager chart. See https://github.com/cert-manager/cert-manager
# for the release matrix.
CERT_MANAGER_CHART_REPO = "https://charts.jetstack.io"
CERT_MANAGER_CHART_NAME = "cert-manager"
CERT_MANAGER_CHART_VERSION = "v1.16.2"

# Conventional namespace. Not configurable: downstream consumers that
# care about cert-manager's presence (CAPI operator, future Issuers)
# assume the standard ``cert-manager`` namespace.
CERT_MANAGER_NAMESPACE = "cert-manager"


class CertManager(pulumi.ComponentResource):
    """Namespace + Helm release of cert-manager (CRDs included).

    Children:
      * ``Namespace/cert-manager``
      * ``helm.sh/v3:Release`` of the pinned chart.

    Outputs:
      * ``namespace`` — chart namespace name (constant, surfaced as an
        Output for DAG-edge tracking).
      * ``release_status`` — the Release's ``status`` Output, anchored so
        downstream resources that need webhooks (CAPI operator, Issuers)
        can wait for cert-manager to be Ready.
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
        super().__init__("ca4s:control_plane:CertManager", name, props={}, opts=opts)

        ns = k8s.core.v1.Namespace(
            f"{name}-ns",
            metadata={"name": CERT_MANAGER_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # Strict upgrade semantics — same rationale as the PKO/ESO
        # releases: roll back failed upgrades atomically, wait for jobs,
        # clean up partial state so a later destroy doesn't strand it.
        release = k8s.helm.v3.Release(
            f"{name}-helm",
            chart=CERT_MANAGER_CHART_NAME,
            version=CERT_MANAGER_CHART_VERSION,
            repository_opts={"repo": CERT_MANAGER_CHART_REPO},
            namespace=CERT_MANAGER_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            # Manage the CRDs as part of this release so they share its
            # lifecycle (cert-manager v1.15+ value name).
            values={"crds": {"enabled": True}},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[ns],
            ),
        )

        self.namespace = pulumi.Output.from_input(CERT_MANAGER_NAMESPACE)
        self.release_status = release.status

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
            }
        )
