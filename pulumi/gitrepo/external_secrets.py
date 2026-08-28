# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""External Secrets Operator install for GitOps bootstrap secrets."""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions


EXTERNAL_SECRETS_CHART_REPO = "https://charts.external-secrets.io"
EXTERNAL_SECRETS_CHART_NAME = "external-secrets"
EXTERNAL_SECRETS_CHART_VERSION = "2.6.0"
EXTERNAL_SECRETS_NAMESPACE = "external-secrets"
_BOOTSTRAP_HELM_TIMEOUT_SECONDS = 30 * 60
_BOOTSTRAP_HELM_TIMEOUT = "30m"


class ExternalSecretsOperator(pulumi.ComponentResource):
    """Namespace + Helm release of External Secrets Operator."""

    namespace: Output[str]
    release_status: Output[object]

    def __init__(
        self,
        name: str,
        *,
        provider: k8s.Provider,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("ca4s:gitrepo:ExternalSecretsOperator", name, props={}, opts=opts)

        ns = k8s.core.v1.Namespace(
            f"{name}-ns",
            metadata={"name": EXTERNAL_SECRETS_NAMESPACE},
            opts=ResourceOptions(parent=self, provider=provider),
        )

        release = k8s.helm.v3.Release(
            f"{name}-helm",
            chart=EXTERNAL_SECRETS_CHART_NAME,
            version=EXTERNAL_SECRETS_CHART_VERSION,
            repository_opts={"repo": EXTERNAL_SECRETS_CHART_REPO},
            namespace=EXTERNAL_SECRETS_NAMESPACE,
            cleanup_on_fail=True,
            atomic=True,
            wait_for_jobs=True,
            timeout=_BOOTSTRAP_HELM_TIMEOUT_SECONDS,
            values={"installCRDs": True},
            opts=ResourceOptions(
                parent=self,
                provider=provider,
                depends_on=[ns],
                custom_timeouts=pulumi.CustomTimeouts(
                    create=_BOOTSTRAP_HELM_TIMEOUT,
                    update=_BOOTSTRAP_HELM_TIMEOUT,
                    delete=_BOOTSTRAP_HELM_TIMEOUT,
                ),
            ),
        )

        self.namespace = Output.from_input(EXTERNAL_SECRETS_NAMESPACE)
        self.release_status = release.status

        self.register_outputs(
            {
                "namespace": self.namespace,
                "release_status": self.release_status,
            }
        )