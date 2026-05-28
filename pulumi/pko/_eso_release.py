"""Helm install of the External Secrets Operator (ESO).

Owns:
  * The ``external-secrets`` Namespace.
  * One ``helm.v3.Release`` of the ``external-secrets`` chart from
    ``https://charts.external-secrets.io`` pinned to
    :data:`ESO_CHART_VERSION`.

ESO is used by :class:`pko._credentials.CredentialsProjection` to
sync the GitOps credentials Secret from the GitOps provider's
namespace into the PKO namespace continuously — so out-of-band
credential rotation (a credential manager patching the upstream
Secret, future Vault-backed creds, chart-driven password rotation)
propagates without waiting for the next ``pulumi up``.

The chart's defaults are already minimal (one controller replica,
CRDs included, bundled validating webhook with self-signed cert).
We accept defaults and only override what we need: nothing yet.

Pin policy mirrors the rest of the stack: explicit chart version
bumps, no implicit "latest". The ``external-secrets.io/v1beta1``
API string in :mod:`pko._credentials` is stable across the 0.9.x —
0.10.x chart line we pin to; 0.11+ promotes ``v1`` and would
require a coordinated apiVersion bump there too.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


# Pinned ESO chart. See
# https://github.com/external-secrets/external-secrets for the
# release matrix.
ESO_CHART_REPO = "https://charts.external-secrets.io"
ESO_CHART_NAME = "external-secrets"
ESO_CHART_VERSION = "0.10.7"

# Conventional namespace + chart-default controller SA name. We
# don't make these configurable because the ``SecretStore`` CR in
# :mod:`pko._credentials` references the controller SA by name when
# granting it TokenRequest RBAC, so renaming would require chart
# value overrides AND a coordinated edit there.
ESO_NAMESPACE = "external-secrets"
ESO_CONTROLLER_SA = "external-secrets"


class ESORelease(pulumi.ComponentResource):
    """Namespace + Helm release of the External Secrets Operator.

    Children:
      * ``Namespace/external-secrets``
      * ``helm.sh/v3:Release`` of the pinned chart.

    Outputs:
      * ``namespace`` — chart namespace name.
      * ``controller_service_account_name`` — chart-default SA name
        the controller pod runs as; consumers grant this SA the
        per-source-namespace RBAC ESO needs (read the source Secret
        via TokenRequest of an impersonated reader SA).
      * ``release_status`` — Release ``status`` Output, anchored so
        downstream CRs wait for the operator (and its CRDs) to be
        Ready.
    """

    namespace: pulumi.Output[str]
    controller_service_account_name: pulumi.Output[str]
    release_status: pulumi.Output[object]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:pko:ESORelease", name, props={}, opts=opts)

        ns = k8s.core.v1.Namespace(
            f"{name}-ns",
            metadata={"name": ESO_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        # Strict upgrade semantics — see :class:`pko._release.PKORelease`
        # for the per-flag rationale; same defaults apply here.
        release = k8s.helm.v3.Release(
            f"{name}-helm",
            chart=ESO_CHART_NAME,
            version=ESO_CHART_VERSION,
            repository_opts={"repo": ESO_CHART_REPO},
            namespace=ESO_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=600,
            # No values overrides. Chart defaults: installCRDs=true,
            # webhook=true (self-signed cert from chart helpers,
            # no cert-manager dependency), 1 controller replica.
            values={},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[ns],
            ),
        )

        self.namespace = pulumi.Output.from_input(ESO_NAMESPACE)
        self.controller_service_account_name = pulumi.Output.from_input(
            ESO_CONTROLLER_SA
        )
        self.release_status = release.status

        self.register_outputs(
            {
                "namespace": self.namespace,
                "controller_service_account_name":
                    self.controller_service_account_name,
                "release_status": self.release_status,
            }
        )
